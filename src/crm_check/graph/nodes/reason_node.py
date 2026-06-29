"""Reason-Node — Pipeline-v2 DOCUMENT-Stufe.

Konsumiert `state.profile: EntityProfile` (vom correlate_node geschrieben) und
formuliert daraus den RowVerdict (Reiter 1) + Enrichment (Reiter 2). Liest
KEINE rohen *_candidates-Listen mehr — die Konfidenz-Hierarchie + Korroboration
ist im profile bereits aggregiert.

OSINT-Konformitaet: jeder FieldVerdict hat ≥1 Source, aktuell=None heisst
"konnten wir nicht pruefen" (nicht "false"), bemerkung benennt Quellen.
"""

from __future__ import annotations

import os
from typing import Any

from crm_check.graph.state import (
    Claim,
    ClaimType,
    CrmCheckState,
    Enrichment,
    EntityProfile,
    FieldVerdict,
    RowVerdict,
    Source,
)


# ─── Claim → Source-Mapping ────────────────────────────────────────────────────

def _claim_to_source(c: Claim) -> Source:
    """Hebt einen Claim auf das Source-Schema (Reiter-1-Beleg)."""
    return Source(
        name=c.source,
        url=c.evidence_url,
        snippet=c.evidence_snippet,
        confidence=c.confidence,
    )


def _best_claim(profile: EntityProfile, ctype: ClaimType) -> Claim | None:
    leaders = profile.claims_by_type.get(ctype) or []
    if not leaders:
        return None
    return max(leaders, key=lambda c: c.confidence)


def _crm_matches(claim_value: str | None, crm_value: str | None) -> bool:
    if not claim_value or not crm_value:
        return False
    a = claim_value.casefold().strip()
    b = crm_value.casefold().strip()
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return short in long_


def _field_verdict_from_claim(
    *,
    field: str,
    crm_value: str | None,
    claim: Claim | None,
    extra_sources: list[Source] | None = None,
) -> FieldVerdict:
    sources: list[Source] = list(extra_sources or [])
    if claim is None:
        return FieldVerdict(
            field=field,  # type: ignore[arg-type]
            status="not_verified",
            crm_value=crm_value,
            sources=sources,
            confidence=0.0,
        )
    src = _claim_to_source(claim)
    sources.append(src)
    # Status: confirmed wenn CRM-Wert passt, changed wenn anders
    if not crm_value:
        status = "confirmed"
        found = claim.value
        note = None
    elif _crm_matches(claim.value, crm_value):
        status = "confirmed"
        found = crm_value
        note = None
    else:
        status = "changed"
        found = claim.value
        note = f"laut Quellen: {claim.value}"
    return FieldVerdict(
        field=field,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        crm_value=crm_value,
        found_value=found,
        note=note,
        sources=sources[:5],
        confidence=round(claim.confidence, 2),
    )


def rule_based_reason(state: CrmCheckState) -> CrmCheckState:
    """Deterministische Verdict-Erzeugung aus state.profile."""
    profile: EntityProfile | None = state.get("profile")
    crm_name = state.get("clean_name") or ""
    crm_position = state.get("position")
    crm_company = state.get("company")

    # Fallback: kein profile (correlate uebersprungen oder Fehler) → leerer Verdict
    if profile is None:
        verdict = RowVerdict(
            aktuell=None,
            bemerkung="Keine Verifikations-Daten verfuegbar (profile fehlt).",
            konfidenz=0.0,
            field_verdicts=[],
        )
        return CrmCheckState(verdict=verdict, enrichment=Enrichment())

    # FieldVerdicts pro relevantem Feld
    person_claim = _best_claim(profile, "person_identity")
    position_claim = _best_claim(profile, "current_position")
    company_claim = _best_claim(profile, "current_employer")
    linkedin_claim = _best_claim(profile, "linkedin_url")

    field_verdicts: list[FieldVerdict] = [
        _field_verdict_from_claim(field="person", crm_value=crm_name, claim=person_claim),
        _field_verdict_from_claim(field="position", crm_value=crm_position, claim=position_claim),
        _field_verdict_from_claim(field="company", crm_value=crm_company, claim=company_claim),
    ]
    if linkedin_claim:
        field_verdicts.append(_field_verdict_from_claim(
            field="linkedin", crm_value=None, claim=linkedin_claim,
        ))

    # WebSearch-Verification-Contradictions in die Bemerkung mitnehmen
    verification = state.get("websearch_verification")
    contradictions: list[str] = []
    if verification and verification.person_confirmed:
        contradictions.extend(verification.contradictions or [])

    # Aggregat
    statuses = {fv.field: fv.status for fv in field_verdicts}
    press_leaders_for_bemerkung = profile.claims_by_type.get("press_mention") or []
    aktuell: bool | None
    if statuses.get("person") == "not_verified":
        aktuell = None
        if press_leaders_for_bemerkung:
            top = max(press_leaders_for_bemerkung, key=lambda c: c.confidence)
            snippet = (top.evidence_snippet or top.value or "")[:140].rstrip()
            bemerkung = (
                f"Kein Personenregister-Treffer; Pressemention via {top.source}: "
                f"{snippet!r}."
            )
        else:
            bemerkung = "Person nicht verifizierbar (keine Tier-1-Quelle traf zu)."
    elif "changed" in statuses.values() or contradictions:
        aktuell = False
        changes = [
            f"{fv.field}: {fv.crm_value!r} → {fv.found_value!r}"
            for fv in field_verdicts
            if fv.status == "changed"
        ]
        changes.extend(contradictions)
        bemerkung = "Abweichung erkannt — " + "; ".join(changes)
    else:
        aktuell = True
        bemerkung = (
            f"Konsistent mit Quellen ({profile.verification_tier}, Score {profile.score})."
        )

    konfidenz = round(profile.score / 100.0, 2)

    verdict = RowVerdict(
        aktuell=aktuell,
        bemerkung=bemerkung,
        konfidenz=konfidenz,
        field_verdicts=field_verdicts,
    )

    # Enrichment (Reiter 2) — aus den besten Claims + NOR + Tier
    enrichment = Enrichment(
        verification_tier=profile.verification_tier,
        score=profile.score,
        nor_status=profile.nor_status,
        nor_note=profile.notes[0] if profile.notes else None,
    )
    if position_claim:
        enrichment.position_now = position_claim.value
    if company_claim:
        enrichment.company_now = company_claim.value
    if linkedin_claim:
        enrichment.linkedin_url = linkedin_claim.value

    # Wikidata-Anreicherung (Wikipedia/Twitter/QID): aus rohen wikidata_hits
    for wd in (state.get("wikidata_hits") or [])[:1]:
        if not enrichment.wikidata_id and getattr(wd, "qid", None):
            enrichment.wikidata_id = wd.qid
        if not enrichment.wikipedia_url and getattr(wd, "wikipedia_url", None):
            enrichment.wikipedia_url = wd.wikipedia_url
        if not enrichment.twitter_url and getattr(wd, "twitter_handle", None):
            handle = wd.twitter_handle
            enrichment.twitter_url = handle if handle.startswith("http") else f"https://x.com/{handle.lstrip('@')}"

    # Letzte Pressemention aus den press_mention-Claims (NI)
    press_leaders = profile.claims_by_type.get("press_mention") or []
    if press_leaders:
        top_press = max(press_leaders, key=lambda c: c.confidence)
        enrichment.last_press_title = top_press.evidence_snippet
        enrichment.last_press_url = top_press.evidence_url

    # Adresse aus address-Claims (OpenRegister)
    address_leaders = profile.claims_by_type.get("address") or []
    if address_leaders:
        enrichment.address_now = max(address_leaders, key=lambda c: c.confidence).value

    # Role-Change-Hinweis aus WebSearch-Verification
    if verification and verification.person_confirmed and verification.contradictions:
        enrichment.role_change_detected = True
        enrichment.role_change_note = "; ".join(verification.contradictions[:2])

    return CrmCheckState(verdict=verdict, enrichment=enrichment)


async def llm_reason(state: CrmCheckState) -> CrmCheckState:
    """Optional: Llama-3.3:70b @ ruediger:11434 verfeinert die Bemerkung.

    Strukturelle Felder bleiben aus rule_based_reason. LLM ueberschreibt nur den
    deutschen Klartext-Satz. Ohne OLLAMA_BASE_URL → reines rule_based_reason.
    """
    base_state = rule_based_reason(state)
    base_url = os.getenv("OLLAMA_BASE_URL")
    if not base_url:
        return base_state

    try:
        import httpx
    except ImportError:
        return base_state

    verdict = base_state.get("verdict")
    if not verdict:
        return base_state

    profile = state.get("profile")
    tier_info = f" (Tier: {profile.verification_tier}, Score: {profile.score}, NOR: {profile.nor_status})" if profile else ""

    prompt = (
        "Du bist ein Datenqualitaets-Assistent fuer eine Wirtschafts-Mailingliste.\n"
        "Konsolidiere folgende Befunde zu EINEM deutschen Klartext-Satz (max 200 Zeichen).\n"
        "Keine Spekulation, nur was in Quellen steht. Wenn unsicher: sag 'unsicher'.\n\n"
        f"CRM-Eintrag: {state.get('clean_name')} — {state.get('position')} @ {state.get('company')}\n"
        f"Aggregat:{tier_info}\n\n"
        "Quellenbefunde:\n"
        + "\n".join(
            f"- {fv.field}: {fv.status} ({fv.found_value or fv.crm_value!r}, "
            f"konf={fv.confidence:.2f}, {len(fv.sources)} Quellen)"
            for fv in verdict.field_verdicts
        )
        + "\n\nDeutscher Klartext-Satz:"
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            resp = await c.post(
                f"{base_url.rstrip('/')}/api/generate",
                json={
                    "model": os.getenv("OLLAMA_MODEL", "llama3.3:70b"),
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": 80},
                },
            )
            resp.raise_for_status()
            text = (resp.json().get("response") or "").strip()
            if text:
                verdict.bemerkung = text
                return CrmCheckState(
                    verdict=verdict,
                    enrichment=base_state.get("enrichment"),
                )
    except Exception as e:
        errs = list(state.get("errors") or []) + [f"llm_reason: {e}"]
        return CrmCheckState(
            verdict=verdict,
            enrichment=base_state.get("enrichment"),
            errors=errs,
        )

    return base_state
