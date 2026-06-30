"""Verify-Node — Llama-3.3:70b @ ruediger:11434 prueft WebSearch-Snippets.

Pipeline-v2-Stufe "VERIFY+QA": Surface-Match in WebSearch-Hits wird nicht
mechanisch als Person-Evidence gewertet, sondern erst durch ein LLM gegen den
CRM-Kontext gepruzft. Ohne diesen Schritt erzeugen Namensvettern + LinkedIn-
Profile mit gleichem Namen falsche "confirmed"-Verdikts (Schreibregeln-/
Memory-Truth-Audit-Verstoss, siehe einspruch 2026-06-29).

Output: WebSearchVerification (state-slot `websearch_verification`).
Ohne OLLAMA_BASE_URL → pass-through (Verification bleibt person_confirmed=False).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable

from crm_check.graph.claims_mapping import verification_to_claims
from crm_check.graph.state import CrmCheckState, WebSearchVerification

log = logging.getLogger(__name__)


NodeFn = Callable[[CrmCheckState], Awaitable[CrmCheckState]]

_VERIFY_PROMPT = """Du bist ein Datenqualitaets-Pruefer fuer eine deutsche Wirtschafts-Mailingliste.
Du arbeitest in ZWEI strikten Stufen — Anti-Confirmation-Bias.

CRM-Eintrag (NUR fuer Stufe B verwenden, NICHT fuer Stufe A):
  Person:   {full_name}
  Position: {position}
  Firma:    {company}
  Ort:      {zip_city}

Web-Suche-Treffer (Title + URL + Snippet):
{hits}

LinkedIn-Profile-Kandidaten (aus site:linkedin.com — auswaehlen welcher zur Person
in Stufe A passt, NULL wenn keiner plausibel ist):
{linkedin_candidates}

═══ STUFE A — Identifikation OHNE Fallbezug ═══
Vergiss die CRM-Felder Position/Firma fuer Stufe A komplett. Schau dir
ausschliesslich die Snippets an und beantworte:
- Wer ist die Person, die da gemeint ist? (Beruf, Branche, prominent wofuer)
- Wie sicher bist du? (0.0 = reine Spekulation, 1.0 = mehrere konsistente Snippets)
Wenn die Snippets verschiedene Personen mit gleichem Namen mischen (Sportler +
Manager): waehle die hochfrequenteste, mit niedrigerer confidence.

═══ STUFE B — Assoziations-Check MIT Fallbezug ═══
Erst JETZT die CRM-Behauptung dazunehmen: ist die in Stufe A identifizierte
Person plausibel der/die {position} bei {company}?
- stage_b_match=true: Stufe-A-Identifikation passt zu Position+Firma
- stage_b_match=false: Stufe-A-Identifikation widerspricht oder beruehrt das Feld nicht

═══ Ableitungen ═══
- person_confirmed = (stage_a_confidence >= 0.5 AND stage_b_match)
- confidence = min(stage_a_confidence, 0.95) wenn stage_b_match sonst 0.3
- role_seen / company_seen / linkedin_url: nur fuellen wenn klar in Snippets
- contradictions: Wechsel-Hinweise ("frueher bei", "wechselte zu", andere Firma)

Antworte EXAKT mit diesem JSON, nichts sonst, kein Markdown:
{{
  "stage_a_identity": <Wer ist Person laut Snippets, 1 Satz oder null>,
  "stage_a_confidence": <0.0-1.0>,
  "stage_b_match": true|false,
  "stage_b_note": <1 Satz Begruendung Stufe B>,
  "person_confirmed": true|false,
  "confidence": <0.0-1.0>,
  "role_seen": <string|null>,
  "company_seen": <string|null>,
  "linkedin_url": <string|null>,
  "evidence_quotes": [<bis zu 3 Zitate>],
  "contradictions": [<0-3 Hinweise>],
  "note": <1 Satz deutsch, max 200 Zeichen>
}}"""


def _extract_linkedin_candidates(websearch_results: list) -> list[str]:
    """Sammelt alle /in/-LinkedIn-Profile-URLs aus den WebSearch-Hits.

    Wir filtern auf `linkedin.com/in/` (Personenprofile), nicht /posts/, /company/,
    /pub/dir/. Das gibt dem LLM eine Shortlist zum Plausibilitaets-Check, statt
    ihn frei aus Snippets extrahieren zu lassen (Random-10 v5 2026-06-30:
    SearXNG liefert 18 Hits fuer "Kurt Zech site:linkedin.com", LLM extrahiert
    NULL weil unsicher zwischen Namensvettern).
    """
    seen: set[str] = set()
    out: list[str] = []
    for wr in websearch_results:
        for h in wr.hits:
            url = (h.url or "").strip()
            if "linkedin.com/in/" not in url.lower():
                continue
            # Normalisiere trailing slash / query
            base = url.split("?")[0].rstrip("/")
            if base in seen:
                continue
            seen.add(base)
            out.append(base)
            if len(out) >= 5:
                return out
    return out


def _build_hits_block(websearch_results: list, max_hits: int = 8) -> str:
    lines: list[str] = []
    seen_urls: set[str] = set()
    for wr in websearch_results:
        for h in wr.hits:
            url = h.url or ""
            if url in seen_urls:
                continue
            seen_urls.add(url)
            title = (h.title or "").strip().replace("\n", " ")
            snippet = (h.snippet or "").strip().replace("\n", " ")
            if len(snippet) > 400:
                snippet = snippet[:400] + "..."
            lines.append(f"- {title}\n  {url}\n  {snippet}")
            if len(lines) >= max_hits:
                return "\n".join(lines)
    return "\n".join(lines) if lines else "(keine Treffer)"


def _parse_llm_json(text: str) -> dict | None:
    """Extrahiere das erste JSON-Objekt aus dem LLM-Output, robust gegen ```-Wrapper."""
    if not text:
        return None
    # Strip Markdown-Code-Fences falls vorhanden
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    # Erstes balanced {...} matchen
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError as e:
        log.warning(f"verify_node: JSON parse failed: {e}; raw={text[start:end][:200]}")
        return None


def _to_verification(data: dict) -> WebSearchVerification:
    """Mappe LLM-JSON auf das Pydantic-Modell, mit defensiven Defaults."""
    def _str(v) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return str(v) or None

    def _list_str(v) -> list[str]:
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for item in v:
            s = _str(item)
            if s:
                out.append(s[:300])
        return out[:3]

    def _float(key: str, default: float = 0.0) -> float:
        try:
            v = float(data.get(key, default) or default)
        except (TypeError, ValueError):
            v = default
        return max(0.0, min(1.0, v))

    stage_a_conf = _float("stage_a_confidence")
    stage_b_match = bool(data.get("stage_b_match", False))
    # Ableitungen (defensive — LLM kann inkonsistent antworten)
    derived_person_confirmed = stage_a_conf >= 0.5 and stage_b_match
    person_confirmed = bool(data.get("person_confirmed", derived_person_confirmed))
    if person_confirmed and not derived_person_confirmed:
        # LLM widerspricht sich (sagt confirmed aber stage_b_match=false oder stage_a zu schwach) → degraden
        person_confirmed = derived_person_confirmed

    # confidence: LLM-Vorschlag, oder ableiten aus Stufen
    raw_conf = data.get("confidence")
    if raw_conf is None:
        conf = stage_a_conf if stage_b_match else min(stage_a_conf, 0.3)
    else:
        conf = _float("confidence")
    if not person_confirmed:
        conf = min(conf, 0.3)  # ohne Stage-B-Match cap

    return WebSearchVerification(
        person_confirmed=person_confirmed,
        confidence=conf,
        role_seen=_str(data.get("role_seen")),
        company_seen=_str(data.get("company_seen")),
        linkedin_url=_str(data.get("linkedin_url")),
        evidence_quotes=_list_str(data.get("evidence_quotes")),
        contradictions=_list_str(data.get("contradictions")),
        note=(_str(data.get("note")) or "")[:240],
        stage_a_identity=_str(data.get("stage_a_identity")),
        stage_a_confidence=stage_a_conf,
        stage_b_match=stage_b_match,
        stage_b_note=_str(data.get("stage_b_note")),
    )


def make_verify_node() -> NodeFn:
    """Erzeugt den Verify-Node. Ohne OLLAMA_BASE_URL → pass-through."""

    async def node(state: CrmCheckState) -> CrmCheckState:
        websearch = state.get("websearch_results") or []
        # Keine WebSearch-Hits → keine Verifikation noetig
        hits_total = sum(len(wr.hits) for wr in websearch)
        if hits_total == 0:
            return CrmCheckState()

        base_url = os.getenv("OLLAMA_BASE_URL")
        if not base_url:
            # Ohne LLM: Verification bleibt unconfirmed. reason_node verwirft die
            # rohen Hits dann als Person-Evidence.
            return CrmCheckState(
                websearch_verification=WebSearchVerification(
                    person_confirmed=False,
                    confidence=0.0,
                    note="LLM-Verify nicht verfuegbar (OLLAMA_BASE_URL fehlt).",
                )
            )

        try:
            import httpx
        except ImportError:
            return CrmCheckState()

        t0 = time.monotonic()
        li_candidates = _extract_linkedin_candidates(websearch)
        li_block = "\n".join(f"  - {u}" for u in li_candidates) if li_candidates else "  (keine LinkedIn-Personenprofile in den Treffern)"
        prompt = _VERIFY_PROMPT.format(
            full_name=state.get("clean_name") or "?",
            position=state.get("position") or "?",
            company=state.get("company") or "?",
            zip_city=state.get("zip_city") or "?",
            hits=_build_hits_block(websearch),
            linkedin_candidates=li_block,
        )

        # Llama-3.3:70b braucht bei 8x parallel oft >60s pro Call (warm-up + queue).
        # Timeout grosszuegig, Exception-Typ explizit loggen damit stumme Fails
        # (Random-10 v4 2026-06-30: alle 8 Verify-Calls scheiterten ohne sichtbaren
        # Grund) diagnostizierbar sind.
        raw = ""
        ollama_timeout = float(os.getenv("OLLAMA_VERIFY_TIMEOUT", "180"))
        try:
            async with httpx.AsyncClient(timeout=ollama_timeout) as c:
                resp = await c.post(
                    f"{base_url.rstrip('/')}/api/generate",
                    json={
                        "model": os.getenv("OLLAMA_MODEL", "llama3.3:70b"),
                        "prompt": prompt,
                        "stream": False,
                        "format": "json",
                        "options": {
                            "temperature": 0.1,
                            "num_predict": 600,
                        },
                    },
                )
                resp.raise_for_status()
                raw = (resp.json().get("response") or "").strip()
        except Exception as e:
            err_repr = f"{type(e).__name__}: {e!r}"
            log.warning(f"verify_node ollama call failed — {err_repr}")
            return CrmCheckState(
                errors=[f"verify: {err_repr}"],
                websearch_verification=WebSearchVerification(
                    person_confirmed=False,
                    confidence=0.0,
                    note=f"LLM-Verify-Fehler ({type(e).__name__})",
                ),
                timings_ms={"verify": int((time.monotonic() - t0) * 1000)},
            )

        data = _parse_llm_json(raw)
        if not data:
            return CrmCheckState(
                errors=[f"verify: JSON parse fail; raw={raw[:120]!r}"],
                websearch_verification=WebSearchVerification(
                    person_confirmed=False,
                    confidence=0.0,
                    note="LLM-Output nicht parsebar.",
                ),
                timings_ms={"verify": int((time.monotonic() - t0) * 1000)},
            )

        verification = _to_verification(data)
        # Pipeline-v2: Aus verifizierter WebSearch werden Claims (current_position,
        # current_employer, linkedin_url). Surface-Match-Hits ohne person_confirmed
        # produzieren keine Claims — siehe verification_to_claims.
        claims = verification_to_claims(verification)
        return CrmCheckState(
            websearch_verification=verification,
            claims=claims,
            timings_ms={"verify": int((time.monotonic() - t0) * 1000)},
        )

    return node
