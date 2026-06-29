"""CLI für Phase 1a: Excel parsen, Namen normalisieren, KG-Lookup.

Spätere Phasen erweitern um CEQ-/NI-/Web-Search und Excel-Output.
"""

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
                    else "STALE-CEQ" if best.is_stale_ceq
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


@main.command("ceq-check")
@click.argument("xlsx", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--ceq-url", envvar="CEQ_API_URL", required=True,
    help="z.B. http://100.78.225.57:8443 — liest auch $CEQ_API_URL",
)
@click.option(
    "--ceq-token", envvar="CEQ_API_TOKEN", required=True,
    help="Bearer-Token — liest auch $CEQ_API_TOKEN",
)
@click.option("--limit", type=int, default=None)
def ceq_check(xlsx: Path, ceq_url: str, ceq_token: str, limit: int | None) -> None:
    """Parsed Excel + queryt Production-CEQ-API. Zeigt echte Treffer pro Zeile."""
    asyncio.run(_ceq_check_async(xlsx, ceq_url, ceq_token, limit))


async def _ceq_check_async(
    xlsx: Path, url: str, token: str, limit: int | None
) -> None:
    from crm_check.graph.nodes.ceq_lookup import CeqClient, rank_persons_by_company
    from crm_check.normalize import strip_salutation

    rows = list(parse_excel(xlsx))
    if limit:
        rows = rows[:limit]

    async with CeqClient(url, token) as client:
        health = await client.health()
        click.echo(
            f"CEQ-API connected — env={health.get('env')} rows={health.get('rows')} "
            f"max_updated={health.get('max_updated_date')}\n"
        )

        matched = 0
        for c in rows:
            query = strip_salutation(c.salutation_name) or c.name_only
            hits = await client.search_persons(query)
            if not hits:
                click.echo(
                    f"R{c.row_idx:>4}  ·  {c.name_only[:30]:<30}  → CEQ: 0 Treffer"
                )
                continue

            ranked = rank_persons_by_company(hits, c.company)
            best, co_match = ranked[0]
            # Sicher matchen wir wenn (a) Company-Match oder (b) Name vollständig
            name_full_match = best.full_name.casefold().strip() == query.casefold().strip()
            if co_match or name_full_match:
                matched += 1
                updated = best.updated_date or "?"
                role = best.role or "?"
                active = "Y" if best.scraping_active else "N" if best.scraping_active is False else "?"
                until = f" until={best.appointed_until}" if best.appointed_until else ""
                li = "LI" if best.linkedin_url else "-"
                click.echo(
                    f"R{c.row_idx:>4}  ✓  {c.name_only[:28]:<28}  "
                    f"→ CEQ {best.full_name[:28]:<28}  "
                    f"role={role[:18]:<18}  "
                    f"{'CO✓' if co_match else 'CO·'}  "
                    f"scrape={active}  {li}  upd={updated}{until}"
                )
            else:
                # Nur Trigram-Treffer, aber kein Name/Company-Match
                near = best.full_name
                click.echo(
                    f"R{c.row_idx:>4}  ?  {c.name_only[:28]:<28}  "
                    f"→ CEQ kein klarer Match (best: {near})"
                )

        click.echo(f"\nSummary: {matched}/{len(rows)} mit hoher Konfidenz gematched.")


@main.command("run")
@click.argument("xlsx", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path),
              default=Path("/tmp/crm_check_out.xlsx"),
              help="Output-Excel-Pfad (default /tmp/crm_check_out.xlsx)")
@click.option("--kg-dsn", envvar="KG_PG_DSN")
@click.option("--ni-dsn", envvar="NI_PG_DSN")
@click.option("--wraite-dsn", envvar="WRAITE_DSN",
              help="wraite Cloud-SQL DSN (oder via WRAITE_DB_* env-vars)")
@click.option("--ceq-url", envvar="CEQ_API_URL")
@click.option("--ceq-token", envvar="CEQ_API_TOKEN")
@click.option("--llm/--no-llm", default=False,
              help="Llama-3.3:70b @ ruediger für deutschen Verdict-Satz (sonst rule-based)")
@click.option("--limit", type=int, default=None)
def run_cmd(
    xlsx: Path, out: Path,
    kg_dsn: str | None, ni_dsn: str | None, wraite_dsn: str | None,
    ceq_url: str | None, ceq_token: str | None,
    llm: bool, limit: int | None,
) -> None:
    """Vollständiger agentischer Lauf — LangGraph + alle Quellen → 2-Reiter-Excel."""
    asyncio.run(_run_async(xlsx, out, kg_dsn, ni_dsn, wraite_dsn,
                           ceq_url, ceq_token, llm, limit))


async def _run_async(
    xlsx: Path, out: Path,
    kg_dsn: str | None, ni_dsn: str | None, wraite_dsn: str | None,
    ceq_url: str | None, ceq_token: str | None,
    llm: bool, limit: int | None,
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
        f"ceq={'yes' if ceq_url else 'off'} llm={'yes' if llm else 'rule-based'}"
    )

    deps = await GraphDeps.open(
        kg_dsn=kg_dsn, ni_dsn=ni_dsn, wraite_dsn=wraite_dsn,
        ceq_url=ceq_url, ceq_token=ceq_token,
        use_llm_reason=llm,
    )
    graph = build_graph(deps)
    final_states = []
    try:
        for c in rows:
            initial = parse_row(c)
            final = await graph.ainvoke(initial)
            final_states.append(final)
            v = final.get("verdict")
            mark = "✓" if (v and v.aktuell is True) else (
                "✗" if (v and v.aktuell is False) else "?"
            )
            n_srcs = sum(len(fv.sources) for fv in (v.field_verdicts if v else []))
            click.echo(
                f"R{c.row_idx:>4}  {mark}  {c.name_only[:30]:<30} "
                f"konf={v.konfidenz if v else 0:.2f}  srcs={n_srcs}  — {v.bemerkung[:60] if v else '-'}"
            )
        from crm_check.output.excel_writer import write_workbook
        write_workbook(out, final_states)
        click.echo(f"\n→ {out}")
    finally:
        await deps.close()


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
@click.option(
    "--ceq-url", envvar="CEQ_API_URL",
    help="Optional CEQ-API-URL für DAX-/Politik-Treffer.",
)
@click.option(
    "--ceq-token", envvar="CEQ_API_TOKEN",
    help="Optional CEQ-API Bearer-Token.",
)
@click.option("--limit", type=int, default=None)
def live_check(
    xlsx: Path,
    kg_dsn: str | None,
    ni_dsn: str | None,
    ceq_url: str | None,
    ceq_token: str | None,
    limit: int | None,
) -> None:
    """Multi-Source-Live-Check: KG-Lobby + KG-Entity + NI-Entities (+ CEQ optional)."""
    if not (kg_dsn and ni_dsn):
        click.echo(
            "FEHLER: --kg-dsn + --ni-dsn pflicht. Beispiel:\n"
            "  export KG_PG_DSN=postgres://kg_api:kg_api_2026@localhost:55438/knowledge_graph\n"
            "  export NI_PG_DSN=postgres://ni:rss_analytics_2026@localhost:55436/news_intelligence",
            err=True,
        )
        sys.exit(2)
    asyncio.run(
        _live_check_async(xlsx, kg_dsn, ni_dsn, ceq_url, ceq_token, limit)
    )


async def _live_check_async(
    xlsx: Path,
    kg_dsn: str,
    ni_dsn: str,
    ceq_url: str | None,
    ceq_token: str | None,
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
    ceq_client = None
    if ceq_url and ceq_token:
        from crm_check.graph.nodes.ceq_lookup import CeqClient
        ceq_client = CeqClient(ceq_url, ceq_token)
        await ceq_client.__aenter__()

    try:
        click.echo(
            f"Sources: kg={kg_dsn.split('@')[-1]} ni={ni_dsn.split('@')[-1]} "
            f"ceq={'yes' if ceq_client else 'off'}\n"
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
            if ceq_client:
                hits = await ceq_client.search_persons(clean)
                if hits:
                    h = hits[0]
                    click.echo(
                        f"        CEQ       {h.full_name[:34]:<34} "
                        f"role={(h.role or '-')[:22]:<22} "
                        f"co={h.company_name or '-'}"
                    )
            if not has_hit and not (ceq_client and hits):
                click.echo("        —  kein Treffer in KG/NI/CEQ")

        click.echo(f"\nSummary: {total_hits}/{len(rows)} mit ≥1 Treffer in KG/NI.")
    finally:
        if ceq_client:
            await ceq_client.__aexit__(None, None, None)
        await ni.close()
        await kg.close()


if __name__ == "__main__":
    main()
