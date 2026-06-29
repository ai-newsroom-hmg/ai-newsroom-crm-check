"""Lookup-Candidate → Claim-Mapping.

Pro Quelle eine Mapper-Funktion, die einen Roh-Candidate (Pydantic-Modell aus
dem jeweiligen Lookup) in 1-N atomare Claims hebt. Source-interne Boosts (z.B.
CEQ scraping_active, OpenRegister active+city_match, KG is_active) werden hier
vergeben — Korroborations-Boosts und Contradiction-Penalties macht der
correlate_node.

Plausibilitaets-Check (Last-Name exact + First-Name 3-Char-Prefix) findet
weiter im Lookup-Node statt; Mapper geht davon aus dass der Candidate bereits
plausibel ist.
"""

from __future__ import annotations

import logging
from typing import Any

from crm_check.graph.scoring import base_confidence, max_boost
from crm_check.graph.state import Claim, ClaimType, SourceName

log = logging.getLogger(__name__)


def _mk(
    *,
    claim_type: ClaimType,
    value: str | None,
    source: SourceName,
    boost: float = 0.0,
    evidence_url: str | None = None,
    evidence_snippet: str | None = None,
    extraction_method: str = "api",
) -> Claim | None:
    """Erzeugt einen Claim; gibt None zurueck wenn value leer (kein Claim ohne Wert)."""
    if not value:
        return None
    val = str(value).strip()
    if not val:
        return None
    return Claim(
        claim_type=claim_type,
        value=val,
        source=source,
        base_confidence=base_confidence(source),
        boost=min(boost, max_boost(source)),
        evidence_url=evidence_url,
        evidence_snippet=evidence_snippet,
        extraction_method=extraction_method,  # type: ignore[arg-type]
    )


# ─── KG person_universe ───────────────────────────────────────────────────────

def kg_to_claims(c: Any) -> list[Claim]:
    """KgCandidate (kg.person_universe) → bis zu 4 Claims."""
    src: SourceName = "kg_person_universe"
    snippet = f"{c.full_name} — {c.role or '?'} @ {c.primary_org or '?'}"
    # is_active=True bzw. nicht-stale-Flags geben Boost
    extra_boost = 0.0
    if getattr(c, "is_active", False):
        extra_boost += 0.10
    if not getattr(c, "is_stale_linkedin", True):
        extra_boost += 0.05
    if getattr(c, "company_match", False):
        extra_boost += 0.05

    out: list[Claim] = []
    out.append(_mk(
        claim_type="person_identity", value=c.full_name, source=src,
        boost=extra_boost, evidence_snippet=snippet, extraction_method="trigram",
    ))
    if c.role:
        out.append(_mk(claim_type="current_position", value=c.role, source=src,
                       boost=extra_boost, evidence_snippet=snippet, extraction_method="trigram"))
    if c.primary_org:
        out.append(_mk(claim_type="current_employer", value=c.primary_org, source=src,
                       boost=extra_boost, evidence_snippet=snippet, extraction_method="trigram"))
    if c.linkedin_url:
        out.append(_mk(claim_type="linkedin_url", value=c.linkedin_url, source=src,
                       boost=extra_boost, evidence_url=c.linkedin_url, extraction_method="api"))
    return [x for x in out if x is not None]


# ─── KG lobby_persons + kg.entities ──────────────────────────────────────────

def kg_lobby_to_claims(lb: Any) -> list[Claim]:
    """KgLobbyCandidate → person_identity + position + employer."""
    src: SourceName = "kg_lobby_persons"
    full_name = " ".join(filter(None, [lb.first_name, lb.last_name])).strip()
    snippet = f"{full_name} — {lb.function or lb.role} ({lb.org_name or 'Lobbyregister'})"
    boost = 0.10 if getattr(lb, "company_match", False) else 0.0
    out: list[Claim] = [
        _mk(claim_type="person_identity", value=full_name, source=src,
            boost=boost, evidence_snippet=snippet, extraction_method="trigram"),
    ]
    role_value = lb.function or lb.role
    if role_value:
        out.append(_mk(claim_type="current_position", value=role_value, source=src,
                       boost=boost, evidence_snippet=snippet, extraction_method="trigram"))
    if lb.org_name:
        out.append(_mk(claim_type="current_employer", value=lb.org_name, source=src,
                       boost=boost, evidence_snippet=snippet, extraction_method="trigram"))
    return [x for x in out if x is not None]


def kg_entity_to_claims(ke: Any) -> list[Claim]:
    """KgEntityCandidate — nur person_identity, schwaecher als lobby."""
    src: SourceName = "kg_entities"
    snippet = f"PERSON-Entity '{ke.canonical_name}' ({ke.total_mentions} mentions)"
    c = _mk(
        claim_type="person_identity", value=ke.canonical_name, source=src,
        evidence_snippet=snippet, extraction_method="trigram",
    )
    return [c] if c else []


# ─── NI mentions ──────────────────────────────────────────────────────────────

def ni_to_claims(n: Any) -> list[Claim]:
    """NiCandidate (Pressemention) → person_identity + press_mention + ggf. position/employer.

    press_mention ist das NOR-Stufe-B-Signal: Person wurde in Nicht-Genios-Presse
    erwaehnt. company_match boost: +0.10. Position-Tokens werden NICHT mehr als
    Person-Identifier verwendet (Confirmation-Bias-Verbot, Pipeline-v2-Refactor).
    """
    src_id: SourceName = "ni_entities"
    src_mention: SourceName = "ni_mentions"
    snippet = f"{n.name} — {n.mention_count} mentions, last: {n.last_article_title or '?'}"
    boost = 0.10 if getattr(n, "company_match", False) else 0.0

    out: list[Claim] = [
        _mk(claim_type="person_identity", value=n.name, source=src_id,
            boost=boost, evidence_snippet=snippet, extraction_method="trigram"),
        _mk(claim_type="press_mention", value=n.last_article_title or n.name,
            source=src_mention, boost=boost,
            evidence_url=n.last_article_url,
            evidence_snippet=n.last_article_title or snippet,
            extraction_method="api"),
    ]
    if n.role:
        out.append(_mk(claim_type="current_position", value=n.role, source=src_mention,
                       boost=boost, evidence_snippet=snippet, extraction_method="llm"))
    if n.primary_org:
        out.append(_mk(claim_type="current_employer", value=n.primary_org, source=src_mention,
                       boost=boost, evidence_snippet=snippet, extraction_method="llm"))
    return [x for x in out if x is not None]


# ─── OpenRegister persons + companies ────────────────────────────────────────

def openregister_person_to_claims(p: Any) -> list[Claim]:
    """OpenRegisterPersonHit → person_identity + (via associations) position/employer."""
    src: SourceName = "openregister"
    snippet = f"{p.full_name} ({p.city or '?'}, active={p.active})"
    boost = 0.0
    if getattr(p, "active", False):
        boost += 0.15
    if p.score and p.score > 0.8:
        boost += 0.10

    out: list[Claim] = [
        _mk(claim_type="person_identity", value=p.full_name, source=src,
            boost=boost, evidence_snippet=snippet, extraction_method="api"),
    ]
    for assoc in (getattr(p, "associations", None) or [])[:3]:
        if isinstance(assoc, dict):
            org = assoc.get("company_name") or assoc.get("org_name")
            role = assoc.get("role") or assoc.get("position")
            if role:
                out.append(_mk(claim_type="current_position", value=str(role), source=src,
                               boost=boost, evidence_snippet=snippet, extraction_method="api"))
            if org:
                out.append(_mk(claim_type="current_employer", value=str(org), source=src,
                               boost=boost, evidence_snippet=snippet, extraction_method="api"))
    return [x for x in out if x is not None]


def openregister_company_to_claims(c: Any) -> list[Claim]:
    """OpenRegisterCandidate.person_match → person_identity + employer + address."""
    src: SourceName = "openregister"
    pm = getattr(c, "person_match", None)
    if not pm:
        return []
    snippet = f"Officer {pm.name} @ {c.company_name} ({pm.role or '?'})"
    boost = 0.15 if getattr(c, "address_match", False) else 0.05
    out: list[Claim] = [
        _mk(claim_type="person_identity", value=pm.name, source=src,
            boost=boost, evidence_snippet=snippet, extraction_method="api"),
        _mk(claim_type="current_employer", value=c.company_name, source=src,
            boost=boost, evidence_snippet=snippet, extraction_method="api"),
    ]
    if pm.role:
        out.append(_mk(claim_type="current_position", value=pm.role, source=src,
                       boost=boost, evidence_snippet=snippet, extraction_method="api"))
    if c.registered_address:
        out.append(_mk(claim_type="address", value=c.registered_address, source=src,
                       boost=boost, evidence_snippet=snippet, extraction_method="api"))
    return [x for x in out if x is not None]


# ─── Wikidata ─────────────────────────────────────────────────────────────────

def wikidata_to_claims(wd: Any) -> list[Claim]:
    """WikidataPersonHit → person_identity + position + employer + linkedin + wikipedia."""
    src: SourceName = "wikidata"
    snippet = f"{wd.label}: {wd.current_position or '?'} @ {wd.current_employer or '?'}"
    url = f"https://www.wikidata.org/wiki/{wd.qid}" if wd.qid else None
    out: list[Claim] = [
        _mk(claim_type="person_identity", value=wd.label, source=src,
            evidence_url=url, evidence_snippet=snippet, extraction_method="sparql"),
    ]
    if wd.current_position:
        out.append(_mk(claim_type="current_position", value=wd.current_position, source=src,
                       evidence_url=url, evidence_snippet=snippet, extraction_method="sparql"))
    if wd.current_employer:
        out.append(_mk(claim_type="current_employer", value=wd.current_employer, source=src,
                       evidence_url=url, evidence_snippet=snippet, extraction_method="sparql"))
    if wd.linkedin_id:
        ld_url = wd.linkedin_id if wd.linkedin_id.startswith("http") else f"https://www.linkedin.com/in/{wd.linkedin_id}/"
        out.append(_mk(claim_type="linkedin_url", value=ld_url, source=src,
                       evidence_url=ld_url, extraction_method="sparql"))
    return [x for x in out if x is not None]


# ─── PressRelations (wraite Cloud-SQL, Tier 2) ───────────────────────────────

def press_relations_to_claims(h: Any) -> list[Claim]:
    """PressRelationsHit → press_mention-Claim.

    PressRelations liefert NUR Mentions (Article-Metadata), keine strukturierten
    Person-Rollen. Wir mappen daher EINEN press_mention-Claim pro Treffer.

    company_match boost: +0.10 (Firma im Headline/Snippet) — analog ni_node.
    """
    src: SourceName = "pressrelations"
    if h is None:
        return []
    headline = getattr(h, "headline", None) or ""
    snippet = getattr(h, "snippet", None) or headline
    url = getattr(h, "url", None)
    boost = 0.10 if getattr(h, "company_match", False) else 0.0

    out: list[Claim] = []
    if headline:
        out.append(_mk(
            claim_type="press_mention", value=headline, source=src,
            boost=boost, evidence_url=url, evidence_snippet=snippet,
            extraction_method="api",
        ))
    return [x for x in out if x is not None]


# ─── WebSearch (nur nach LLM-Verify) ─────────────────────────────────────────

def verification_to_claims(v: Any) -> list[Claim]:
    """WebSearchVerification → Claims nur wenn person_confirmed.

    Surface-Match-Verbot: rohe WebSearch-Hits werden NICHT zu Claims. Erst nach
    Llama-Bestaetigung (verify_node) zaehlt der Web-Befund als Quelle.
    """
    if not v or not getattr(v, "person_confirmed", False):
        return []
    src: SourceName = "perplexity"
    confidence = getattr(v, "confidence", 0.0)
    if confidence < 0.5:
        return []
    note = getattr(v, "note", "") or ""
    quotes = getattr(v, "evidence_quotes", None) or []
    snippet = note or (quotes[0] if quotes else None)

    out: list[Claim] = []
    if v.role_seen:
        out.append(_mk(claim_type="current_position", value=v.role_seen, source=src,
                       evidence_snippet=snippet, extraction_method="llm"))
    if v.company_seen:
        out.append(_mk(claim_type="current_employer", value=v.company_seen, source=src,
                       evidence_snippet=snippet, extraction_method="llm"))
    if v.linkedin_url:
        out.append(_mk(claim_type="linkedin_url", value=v.linkedin_url, source=src,
                       evidence_url=v.linkedin_url, extraction_method="llm"))
    return [x for x in out if x is not None]
