"""CLI für CRM-Check: Excel parsen, Multi-Source-Lookup, Verdict, Excel-Output."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import click

from crm_check.normalize import name_for_matching
from crm_check.parser import parse_excel


@click.group()
def main() -> None:
    """CRM-Check — Mailing-Listen-Aktualität gegen ai-newsroom Entity-Intelligence."""


@main.command()
@click.argument("xlsx", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--limit", type=int, default=None, help="Nur die ersten N Zeilen.")
def parse(xlsx: Path, limit: int | None) -> None:
    """Parsed eine Excel und zeigt Zeile + Normalized-Match-Key."""
    rows = list(parse_excel(xlsx))
    if limit:
        rows = rows[:limit]

    for c in rows:
        click.echo(
            f"R{c.row_idx:>4}  "
            f"{c.display:<70.70}  "
            f"→ match-key: {name_for_matching(c.salutation_name)!r}"
        )
    click.echo(f"\n{len(rows)} Zeilen geparst.")


@main.command()
@click.argument("xlsx", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--kg-dsn",
    envvar="KG_PG_DSN",
    help="Postgres-DSN für kg.person_universe (Pflicht). Liest auch $KG_PG_DSN.",
)
@click.option("--limit", type=int, default=None, help="Nur die ersten N Zeilen.")
@click.option("--per-row-candidates", type=int, default=3, help="Top-K aus KG pro Zeile.")
def check(xlsx: Path, kg_dsn: str | None, limit: int | None, per_row_candidates: int) -> None:
    """Parst Excel + holt Top-K KG-Kandidaten pro Zeile. Output als kompakter Report."""
    if not kg_dsn:
        click.echo(
            "FEHLER: --kg-dsn fehlt und $KG_PG_DSN ist nicht gesetzt.\n"
            "Lokal: docker-compose up -d kg-postgres && "
            "export KG_PG_DSN=postgres://kg:kg_dev_only@localhost:55432/knowledge_graph",
            err=True,
        )
        sys.exit(2)

    asyncio.run(_check_async(xlsx, kg_dsn, limit, per_row_candidates))


async def _check_async(
    xlsx: Path, dsn: str, limit: int | None, per_row_candidates: int
) -> None:
    import asyncpg

    from crm_check.graph.nodes.kg_lookup import lookup_kg

    rows = list(parse_excel(xlsx))
    if limit:
        rows = rows[:limit]

    conn = await asyncpg.connect(dsn)
    try:
        matched = 0
        unmatched = 0
        for c in rows:
            cands = await lookup_kg(
                conn, c.salutation_name, company=c.company, limit=per_row_candidates
            )
            best = cands[0] if cands else None
            if best and best.similarity_score >= 0.5:
                matched += 1
                staleness = (
                    "STALE-LI" if best.is_stale_linkedin
                    else "STALE-WD" if best.is_stale_wikidata
                    else "fresh"
                )
                click.echo(
                    f"R{c.row_idx:>4}  ✓  {c.name_only[:30]:<30}  "
                    f"→ KG#{best.person_id} {best.full_name[:30]:<30}  "
                    f"sim={best.similarity_score:.2f}  "
                    f"{'CO✓' if best.company_match else 'CO·'}  "
                    f"active={'Y' if best.is_active else 'N'}  "
                    f"{staleness}"
                )
            else:
                unmatched += 1
                click.echo(
                    f"R{c.row_idx:>4}  ·  {c.name_only[:30]:<30}  → kein KG-Match"
                )
        click.echo(
            f"\nSummary: {matched}/{len(rows)} matched  ({unmatched} unmatched)"
        )
    finally:
        await conn.close()



@main.command("run")
@click.argument("xlsx", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path),
              default=Path("/tmp/crm_check_out.xlsx"),
              help="Output-Excel-Pfad (default /tmp/crm_check_out.xlsx)")
@click.option("--kg-dsn", envvar="KG_PG_DSN")
@click.option("--ni-dsn", envvar="NI_PG_DSN")
@click.option("--wraite-dsn", envvar="WRAITE_DSN",
              help="wraite Cloud-SQL DSN (oder via WRAITE_DB_* env-vars)")
@click.option("--hugoplus-user", envvar="HUGOPLUS_USER")
@click.option("--hugoplus-pass", envvar="HUGOPLUS_PASS")
@click.option("--llm/--no-llm", default=False,
              help="Llama-3.3:70b @ ruediger für deutschen Verdict-Satz (sonst rule-based)")
@click.option("--audit-jsonl", type=click.Path(dir_okay=False, path_type=Path),
              default=None,
              help="JSONL-Audit-Trail pro Zeile (Halluzinations-Sicherheit). "
                   "Default: {out}.audit.jsonl neben dem Output.")
@click.option("--concurrency", type=int, default=16,
              help="Anzahl paralleler Zeilen-Pipelines (asyncio.Semaphore). "
                   "Default 16 — Ollama NUM_PARALLEL sollte matchen.")
@click.option("--limit", type=int, default=None)
def run_cmd(
    xlsx: Path, out: Path,
    kg_dsn: str | None, ni_dsn: str | None, wraite_dsn: str | None,
    hugoplus_user: str | None, hugoplus_pass: str | None,
    llm: bool, audit_jsonl: Path | None, concurrency: int, limit: int | None,
) -> None:
    """Vollständiger agentischer Lauf — LangGraph + alle Quellen → 2-Reiter-Excel."""
    asyncio.run(_run_async(xlsx, out, kg_dsn, ni_dsn, wraite_dsn,
                           hugoplus_user, hugoplus_pass, llm, audit_jsonl,
                           concurrency, limit))


async def _run_async(
    xlsx: Path, out: Path,
    kg_dsn: str | None, ni_dsn: str | None, wraite_dsn: str | None,
    hugoplus_user: str | None, hugoplus_pass: str | None,
    llm: bool, audit_jsonl: Path | None,
    concurrency: int, limit: int | None,
) -> None:
    from crm_check.graph.build import GraphDeps, build_graph
    from crm_check.graph.nodes.parse_node import parse_row

    rows = list(parse_excel(xlsx))
    if limit:
        rows = rows[:limit]

    # WRAITE_DSN aus 5 Einzel-ENV-Variablen zusammensetzen wenn nicht direkt gesetzt
    if not wraite_dsn:
        import os as _os
        wh = _os.getenv("WRAITE_DB_HOST", "")
        wpw = _os.getenv("WRAITE_DB_PASSWORD", "")
        if wh and wpw:
            wp = _os.getenv("WRAITE_DB_PORT", "5434")
            wn = _os.getenv("WRAITE_DB_NAME", "postgres")
            wu = _os.getenv("WRAITE_DB_USER", "gunterclaude")
            wraite_dsn = f"postgresql://{wu}:{wpw}@{wh}:{wp}/{wn}"

    click.echo(
        f"Quellen: kg={'yes' if kg_dsn else 'off'} ni={'yes' if ni_dsn else 'off'} "
        f"wraite={'yes' if wraite_dsn else 'off'} "
        f"hugo={'yes' if (hugoplus_user and hugoplus_pass) else 'off'} "
        f"llm={'yes' if llm else 'rule-based'}"
    )

    deps = await GraphDeps.open(
        kg_dsn=kg_dsn, ni_dsn=ni_dsn, wraite_dsn=wraite_dsn,
        hugoplus_user=hugoplus_user, hugoplus_pass=hugoplus_pass,
        use_llm_reason=llm,
    )
    graph = build_graph(deps)

    audit_path = audit_jsonl or out.with_suffix(out.suffix + ".audit.jsonl")
    audit_fp = audit_path.open("w", encoding="utf-8")
    audit_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, concurrency))
    click.echo(f"Audit-JSONL: {audit_path}  | concurrency={concurrency}")

    progress = {"done": 0, "total": len(rows), "t0": __import__("time").monotonic()}

    async def _process(c) -> tuple[int, dict]:
        async with sem:
            initial = parse_row(c)
            try:
                final = await graph.ainvoke(initial)
            except Exception as e:
                # Graceful: eine Zeile darf nicht den Voll-Run kippen
                final = {"errors": [f"graph_invoke: {e}"], "row_idx": c.row_idx}
            v = final.get("verdict") if isinstance(final, dict) else None
            mark = "✓" if (v and v.aktuell is True) else (
                "✗" if (v and v.aktuell is False) else "?"
            )
            n_srcs = sum(len(fv.sources) for fv in (v.field_verdicts if v else []))
            progress["done"] += 1
            n_done = progress["done"]
            elapsed = __import__("time").monotonic() - progress["t0"]
            rate = n_done / max(elapsed, 0.1)
            eta = (progress["total"] - n_done) / max(rate, 0.01)
            click.echo(
                f"[{n_done:>4}/{progress['total']}  {rate:5.2f}r/s  ETA {int(eta):>4}s] "
                f"R{c.row_idx:>4}  {mark}  {c.name_only[:28]:<28} "
                f"k={v.konfidenz if v else 0:.2f} s={n_srcs}  — "
                f"{(v.bemerkung[:60] if v else '-')}"
            )
            async with audit_lock:
                _emit_audit_record(audit_fp, c, final)
                audit_fp.flush()
            return c.row_idx, final

    try:
        results = await asyncio.gather(*(_process(c) for c in rows))
        # Reihenfolge wiederherstellen (Semaphore + gather können out-of-order completen)
        results.sort(key=lambda t: t[0])
        final_states = [f for _, f in results]
        from crm_check.output.excel_writer import write_workbook
        write_workbook(out, final_states)
        click.echo(f"\n→ {out}")
    finally:
        audit_fp.close()
        await deps.close()


def _emit_audit_record(fp, contact, final_state) -> None:
    """Schreibt einen Halluzinations-Audit-Eintrag pro Excel-Zeile.

    Ein JSONL-Record je Zeile mit:
      - Eingabe (row_idx, salutation_name, company)
      - Verdict (aktuell, konfidenz, bemerkung, tier)
      - Alle Claims (source, claim_type, value, confidence, evidence_url, snippet)
      - Profile-Score / NOR-Status
    Damit ist jede Verdict-Behauptung gegen ihre Originalquelle (URL + Snippet)
    nachvollziehbar. Llama-Sätze ohne Claim-Backing fallen so auf.
    """
    import json
    from datetime import datetime

    v = final_state.get("verdict")
    profile = final_state.get("profile")
    claims = final_state.get("claims") or []

    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "row_idx": contact.row_idx,
        "input": {
            "salutation_name": contact.salutation_name or "",
            "name_only": contact.name_only or "",
            "company": contact.company or "",
        },
        "verdict": (
            {
                "aktuell": v.aktuell,
                "konfidenz": v.konfidenz,
                "bemerkung": v.bemerkung,
                "tier": getattr(v, "verification_tier", None),
            }
            if v else None
        ),
        "profile": (
            {
                "score": profile.score,
                "tier": profile.verification_tier,
                "nor_status": profile.nor_status,
                "nor_score": profile.nor_score,
            }
            if profile else None
        ),
        "claims": [
            {
                "source": getattr(cl, "source", None),
                "claim_type": getattr(cl, "claim_type", None),
                "value": getattr(cl, "value", None),
                "base_confidence": getattr(cl, "base_confidence", None),
                "boost": getattr(cl, "boost", None),
                "confidence": getattr(cl, "confidence", None),
                "evidence_url": getattr(cl, "evidence_url", None),
                "evidence_snippet": (
                    (getattr(cl, "evidence_snippet", "") or "")[:240] or None
                ),
                "extraction_method": getattr(cl, "extraction_method", None),
            }
            for cl in claims
        ],
        "match_gate_decisions": final_state.get("match_gate_decisions") or [],
    }
    fp.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


@main.command("live-check")
@click.argument("xlsx", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--kg-dsn", envvar="KG_PG_DSN",
    help="Postgres-DSN für kg (lobby_persons + entities). Liest $KG_PG_DSN.",
)
@click.option(
    "--ni-dsn", envvar="NI_PG_DSN",
    help="Postgres-DSN für ni (entities + entity_profiles). Liest $NI_PG_DSN.",
)
@click.option("--limit", type=int, default=None)
def live_check(
    xlsx: Path,
    kg_dsn: str | None,
    ni_dsn: str | None,
    limit: int | None,
) -> None:
    """Multi-Source-Live-Check: KG-Lobby + KG-Entity + NI-Entities."""
    if not (kg_dsn and ni_dsn):
        click.echo(
            "FEHLER: --kg-dsn + --ni-dsn pflicht. Beispiel:\n"
            "  export KG_PG_DSN=postgres://kg_api:kg_api_2026@localhost:55438/knowledge_graph\n"
            "  export NI_PG_DSN=postgres://ni:rss_analytics_2026@localhost:55436/news_intelligence",
            err=True,
        )
        sys.exit(2)
    asyncio.run(
        _live_check_async(xlsx, kg_dsn, ni_dsn, limit)
    )


async def _live_check_async(
    xlsx: Path,
    kg_dsn: str,
    ni_dsn: str,
    limit: int | None,
) -> None:
    import asyncpg

    from crm_check.graph.nodes.kg_lobby_lookup import (
        lookup_kg_entity,
        lookup_kg_lobby,
    )
    from crm_check.graph.nodes.ni_lookup import lookup_ni, rank_with_company
    from crm_check.normalize import strip_salutation

    rows = list(parse_excel(xlsx))
    if limit:
        rows = rows[:limit]

    kg = await asyncpg.connect(kg_dsn)
    ni = await asyncpg.connect(ni_dsn)

    try:
        click.echo(
            f"Sources: kg={kg_dsn.split('@')[-1]} ni={ni_dsn.split('@')[-1]} "
        )
        total_hits = 0
        for c in rows:
            clean = strip_salutation(c.salutation_name) or c.name_only
            parts = clean.split()
            last = parts[-1] if parts else ""

            kg_lobby = await lookup_kg_lobby(kg, last, company=c.company, limit=3)
            kg_lobby = [k for k in kg_lobby if k.similarity_score >= 0.3]

            kg_ent = await lookup_kg_entity(kg, clean, limit=3)
            kg_ent = [k for k in kg_ent if k.similarity_score >= 0.3]

            ni_cands = await lookup_ni(ni, clean, last_name=last, limit=5)
            ni_cands = rank_with_company(ni_cands, c.company or "")

            has_hit = bool(kg_lobby or kg_ent or ni_cands)
            if has_hit:
                total_hits += 1

            click.echo(f"R{c.row_idx:>4}  {clean[:32]:<32}  ({c.company[:24] if c.company else '-':<24})")
            if kg_lobby:
                lb = kg_lobby[0]
                gov = " GOV" if lb.gov_function_present else ""
                click.echo(
                    f"        KG-LOBBY  {lb.first_name or '?'} {lb.last_name}  "
                    f"role={(lb.function or lb.role)[:34]:<34} "
                    f"org={(lb.org_name or '-')[:24]:<24} sim={lb.similarity_score:.2f}"
                    f"{' CO✓' if lb.company_match else ''}{gov}"
                )
            if kg_ent:
                ke = kg_ent[0]
                click.echo(
                    f"        KG-ENT    {ke.canonical_name[:34]:<34} "
                    f"mentions={ke.total_mentions} sim={ke.similarity_score:.2f} "
                    f"{ke.wikidata_id or '-'}"
                )
            if ni_cands:
                n0 = ni_cands[0]
                tail = ""
                if n0.last_mention_at:
                    tail = f" last={n0.last_mention_at.date().isoformat()} {n0.last_article_domain or ''}"
                click.echo(
                    f"        NI        {n0.name[:34]:<34} "
                    f"role={(n0.role or '-')[:22]:<22} "
                    f"org={(n0.primary_org or '-')[:24]:<24} "
                    f"mentions={n0.mention_count}"
                    f"{' CO✓' if n0.company_match else ''}{tail}"
                )

            if not has_hit:
                click.echo("        —  kein Treffer in KG/NI")

        click.echo(f"\nSummary: {total_hits}/{len(rows)} mit ≥1 Treffer in KG/NI.")
    finally:
        await ni.close()
        await kg.close()


@main.command("audit-stats")
@click.argument("jsonl", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--top-claims", type=int, default=10,
              help="Anzahl Top-Claims (nach Konfidenz) zur Stichproben-Ausgabe.")
def audit_stats(jsonl: Path, top_claims: int) -> None:
    """Aggregat-Statistik aus dem Halluzinations-Audit-JSONL.

    Liefert nach einem Voll-Run sofort das Hit-Rate-Bild: Tier-/NOR-Verteilung,
    Match-Gate-Reject-Rate, Quellen-Counts pro claim_type, mittlere Konfidenz.
    """
    import json
    from collections import Counter, defaultdict

    n_rows = 0
    tier_counts: Counter[str] = Counter()
    nor_counts: Counter[str] = Counter()
    aktuell_counts: Counter[str] = Counter()
    gate_accept = 0
    gate_reject = 0
    gate_rules: Counter[str] = Counter()
    src_by_ct: dict[str, Counter[str]] = defaultdict(Counter)
    conf_by_src: dict[str, list[float]] = defaultdict(list)
    score_sum = 0.0
    score_n = 0
    no_claim_rows = 0
    top_picks: list[tuple[float, int, str, str, str]] = []  # (conf, row, ct, source, snippet)

    with open(jsonl, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_rows += 1
            prof = rec.get("profile") or {}
            v = rec.get("verdict") or {}
            tier_counts[prof.get("tier") or "none"] += 1
            nor_counts[prof.get("nor_status") or "none"] += 1
            score = prof.get("score")
            if isinstance(score, (int, float)):
                score_sum += float(score)
                score_n += 1
            ak = v.get("aktuell")
            aktuell_counts["true" if ak is True else "false" if ak is False else "null"] += 1

            cls = rec.get("claims") or []
            if not cls:
                no_claim_rows += 1
            for cl in cls:
                ct = cl.get("claim_type") or "?"
                src = cl.get("source") or "?"
                src_by_ct[ct][src] += 1
                conf = cl.get("confidence")
                if isinstance(conf, (int, float)):
                    conf_by_src[src].append(float(conf))
                    top_picks.append((
                        float(conf), rec.get("row_idx", -1), ct, src,
                        (cl.get("evidence_snippet") or cl.get("value") or "")[:90],
                    ))

            for d in rec.get("match_gate_decisions") or []:
                if d.get("accepted"):
                    gate_accept += 1
                else:
                    gate_reject += 1
                gate_rules[d.get("rule") or "?"] += 1

    click.echo(f"=== Audit-Stats {jsonl.name} — {n_rows} rows ===\n")

    click.echo("Verdict-aktuell:")
    for k in ("true", "false", "null"):
        click.echo(f"  {k:<5} {aktuell_counts[k]:>5} ({aktuell_counts[k]/max(n_rows,1)*100:5.1f} %)")

    click.echo("\nVerification-Tier:")
    for t in ("confirmed", "probable", "unconfirmed", "none"):
        if tier_counts[t]:
            click.echo(f"  {t:<12} {tier_counts[t]:>5} ({tier_counts[t]/max(n_rows,1)*100:5.1f} %)")

    click.echo("\nNOR-Status:")
    for s in ("public", "nor", "unidentified", "none"):
        if nor_counts[s]:
            click.echo(f"  {s:<12} {nor_counts[s]:>5} ({nor_counts[s]/max(n_rows,1)*100:5.1f} %)")

    avg_score = score_sum / max(score_n, 1)
    click.echo(f"\nDurchschnitts-Score (profile.score): {avg_score:5.1f} (n={score_n})")
    click.echo(f"Zeilen ohne Claim-Treffer: {no_claim_rows}/{n_rows} ({no_claim_rows/max(n_rows,1)*100:.1f} %)")

    total_gate = gate_accept + gate_reject
    if total_gate:
        click.echo(f"\nMatch-Gate: {gate_accept} accept / {gate_reject} reject "
                   f"({gate_reject/total_gate*100:.1f} % reject)")
        for rule, n in gate_rules.most_common():
            click.echo(f"  {rule:<28} {n}")

    click.echo("\nQuellen-Hit-Counts pro claim_type:")
    for ct in sorted(src_by_ct.keys()):
        click.echo(f"  {ct}:")
        for src, n in src_by_ct[ct].most_common():
            confs = conf_by_src.get(src) or []
            avg = sum(confs) / len(confs) if confs else 0.0
            click.echo(f"    {src:<22} {n:>5}  ⌀conf={avg:.2f}")

    if top_picks:
        top_picks.sort(reverse=True)
        click.echo(f"\nTop-{top_claims} Claims nach Konfidenz:")
        for conf, row, ct, src, snip in top_picks[:top_claims]:
            click.echo(f"  R{row:<5} {ct:<18} {src:<20} conf={conf:.2f}  {snip}")


@main.command("preflight")
@click.option("--kg-dsn", envvar="KG_PG_DSN", help="kg.person_universe Postgres-DSN")
@click.option("--ni-dsn", envvar="NI_PG_DSN", help="ni.entities Postgres-DSN")
@click.option("--wraite-dsn", envvar="WRAITE_DSN",
              help="PressRelations Postgres-DSN (oder WRAITE_DB_HOST/PASS Combo)")
@click.option("--openregister-api-key", envvar="OPENREGISTER_API_KEY",
              help="OpenRegister-API-Key")
@click.option("--searxng-url", envvar="SEARXNG_URL", help="SearXNG-Endpoint")
@click.option("--ollama-url", envvar="OLLAMA_BASE_URL", help="Ollama-Endpoint für llama-Verify")
@click.option("--strict/--no-strict", default=True,
              help="--strict: exit 1 wenn Tier-1-Quelle (KG oder NI) fehlt")
def preflight_cmd(kg_dsn, ni_dsn, wraite_dsn, openregister_api_key,
                  searxng_url, ollama_url, strict: bool) -> None:
    """Pre-Flight-Coverage-Check vor jedem Bulk-Run.

    Probt alle Lookup-Quellen mit billigem Health-Query. Skill:
    upstream-source-freshness-preflight. Verhindert blinden Run wie
    2026-06-29 (kg.person_universe nicht angeschlossen → 46 % Claim-leer).
    """
    asyncio.run(_preflight_async(kg_dsn, ni_dsn, wraite_dsn,
                                 openregister_api_key, searxng_url,
                                 ollama_url, strict))


async def _preflight_async(kg_dsn, ni_dsn, wraite_dsn, or_key,
                           searxng_url, ollama_url, strict: bool) -> None:
    import os as _os

    if not wraite_dsn:
        wh = _os.getenv("WRAITE_DB_HOST", "")
        wpw = _os.getenv("WRAITE_DB_PASSWORD", "")
        if wh and wpw:
            wp = _os.getenv("WRAITE_DB_PORT", "5434")
            wn = _os.getenv("WRAITE_DB_NAME", "postgres")
            wu = _os.getenv("WRAITE_DB_USER", "gunterclaude")
            wraite_dsn = f"postgresql://{wu}:{wpw}@{wh}:{wp}/{wn}"

    results: list[tuple[str, str, str, str]] = []  # (name, tier, status, detail)

    async def _probe_pg(name: str, tier: str, dsn: str | None, probe_sql: str) -> None:
        if not dsn:
            results.append((name, tier, "MISSING", "DSN not set"))
            return
        try:
            import asyncpg
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=8.0)
            try:
                row = await asyncio.wait_for(conn.fetchrow(probe_sql), timeout=8.0)
                results.append((name, tier, "OK", str(row)[:80] if row else "empty"))
            finally:
                await conn.close()
        except Exception as e:
            results.append((name, tier, "FAIL", f"{type(e).__name__}: {str(e)[:90]}"))

    async def _probe_http(name: str, tier: str, url: str | None,
                          method: str = "GET", expected_status_min: int = 200) -> None:
        if not url:
            results.append((name, tier, "MISSING", "URL/Key not set"))
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as cli:
                r = await cli.request(method, url)
                ok = expected_status_min <= r.status_code < 500
                results.append((
                    name, tier,
                    "OK" if ok else "FAIL",
                    f"HTTP {r.status_code}",
                ))
        except Exception as e:
            results.append((name, tier, "FAIL", f"{type(e).__name__}: {str(e)[:90]}"))

    # Tier-1: KG (person_universe), NI (entities)
    await _probe_pg(
        "KG person_universe", "Tier-1", kg_dsn,
        "SELECT count(*) FROM kg.person_universe LIMIT 1",
    )
    await _probe_pg(
        "NI entities", "Tier-1", ni_dsn,
        "SELECT count(*) FROM ni.entities LIMIT 1",
    )

    # Tier-2: PressRelations
    await _probe_pg(
        "PressRelations", "Tier-2", wraite_dsn,
        "SELECT current_user, now()",
    )

    # OpenRegister (Tier-1, API)
    if or_key:
        results.append(("OpenRegister", "Tier-1", "OK", f"key set ({or_key[:8]}...)"))
    else:
        results.append(("OpenRegister", "Tier-1", "MISSING", "OPENREGISTER_API_KEY not set"))

    # SearXNG (verify-Hilfe, optional)
    if searxng_url:
        await _probe_http("SearXNG", "Verify", searxng_url + "/")
    else:
        results.append(("SearXNG", "Verify", "MISSING", "SEARXNG_URL not set"))

    # Ollama llama-verify (verify-Hilfe, optional)
    if ollama_url:
        await _probe_http("Ollama", "Verify", ollama_url + "/api/tags")
    else:
        results.append(("Ollama", "Verify", "MISSING", "OLLAMA_BASE_URL not set"))

    click.echo("\n=== Pre-Flight Coverage-Check ===")
    click.echo(f"{'Quelle':<22} {'Tier':<8} {'Status':<8} Detail")
    click.echo("-" * 80)
    tier1_fail = 0
    for name, tier, status, detail in results:
        marker = "✓" if status == "OK" else "✗" if status == "FAIL" else "—"
        click.echo(f"{marker} {name:<20} {tier:<8} {status:<8} {detail}")
        if tier == "Tier-1" and status != "OK":
            tier1_fail += 1

    n_ok = sum(1 for _, _, s, _ in results if s == "OK")
    click.echo("-" * 80)
    click.echo(f"{n_ok}/{len(results)} Quellen verfügbar, {tier1_fail} Tier-1-Lücken.")
    if strict and tier1_fail > 0:
        click.echo("\n✗ STRICT-MODE: Tier-1-Quelle fehlt → Bulk-Run nicht freigegeben.", err=True)
        sys.exit(1)
    click.echo("→ GO für Bulk-Run." if tier1_fail == 0 else "→ DEGRADED (--no-strict bewusst akzeptiert).")


if __name__ == "__main__":
    main()
