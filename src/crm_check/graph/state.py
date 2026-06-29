"""CrmCheckState — TypedDict für den LangGraph-StateGraph.

Pro Excel-Zeile durchläuft ein State alle Nodes (Parse → Normalize → KG/NI/CEQ
parallel → optional OpenRegister/Social/Websearch → Reason → Persist).

Die Nodes hängen nur an ihren Output-Slot — niemand überschreibt Felder von
anderen. Damit kann LangGraph die Lookup-Nodes parallel laufen lassen.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel

# ──────────────────────────────────────────────────────────────────────────────
# Verdict-Schema (OSINT-konform: kein binärer Score, sondern pro Feld + Source)
# ──────────────────────────────────────────────────────────────────────────────

VerdictStatus = Literal["confirmed", "changed", "flagged", "not_verified"]
SourceName = Literal[
    "kg_person_universe",
    "kg_lobby_persons",
    "kg_entities",
    "ni_entities",
    "ni_mentions",
    "ceq_api",
    "openregister",
    "wikidata",
    "linkedin",
    "perplexity",
    "llm_reasoning",
]

# Pipeline-v2: Claim-basiertes Datenmodell + 3-Tier-Verifikation + NOR-Discovery
# Spec: `Konzepte/AI Newsroom/Entity Intelligence – Pipeline v2.md`
ClaimType = Literal[
    "person_identity",     # Diese Person existiert / ist identifiziert
    "current_position",    # Aktuelle Rolle / Funktion
    "current_employer",    # Aktuelle Firma
    "linkedin_url",
    "address",
    "press_mention",       # Person wurde in Presse erwaehnt (NOR-Stufe-B-Signal)
]
NorStatus = Literal["public", "nor", "unidentified"]
VerificationTier = Literal["confirmed", "probable", "unconfirmed"]
ExtractionMethod = Literal["api", "trigram", "sparql", "websearch", "llm"]


class Source(BaseModel):
    """Einzelner Beleg. Jeder Befund muss mindestens eine Source haben."""

    name: SourceName
    url: str | None = None
    snippet: str | None = None
    date: datetime | None = None
    confidence: float = 1.0  # 0.0–1.0, Quellen-spezifisches Vertrauen


class FieldVerdict(BaseModel):
    """Befund für genau ein Feld der CRM-Zeile."""

    field: Literal["person", "position", "company", "address", "linkedin"]
    status: VerdictStatus
    crm_value: str | None = None       # Was in der Mailingliste steht
    found_value: str | None = None     # Was wir live finden (bei changed)
    note: str | None = None            # Deutscher Klartext für die Bemerkung
    sources: list[Source] = []
    confidence: float = 0.0            # 0.0–1.0, aggregiert


class RowVerdict(BaseModel):
    """Aggregierter Befund für die Excel-Zeile (Reiter 1)."""

    aktuell: bool | None                # True/False/None (None = unbekannt)
    bemerkung: str                       # Deutscher Satz
    konfidenz: float                     # 0.0–1.0
    field_verdicts: list[FieldVerdict] = []


class Claim(BaseModel):
    """Pipeline-v2 entity_claim: atomare Behauptung einer Quelle ueber die Person.

    Lookup-Nodes erzeugen Claims statt direkt FieldVerdicts zu bauen. Der
    correlate_node konsolidiert sie zu einem EntityProfile (mit
    Korroborations-/Contradiction-Aggregat).
    """

    claim_type: ClaimType
    value: str                                  # Klartext-Wert (Position, Org-Name, URL, ...)
    source: SourceName
    base_confidence: float                      # aus SOURCE_CONFIDENCE (scoring.py)
    boost: float = 0.0                          # Korroborations-Boost (von correlate_node)
    contradiction_penalty: float = 0.0          # Penalty bei Mismatch
    corroborated_by: list[SourceName] = []      # andere Quellen die diesen Claim stuetzen
    contradicted_by: list[SourceName] = []      # Quellen mit abweichendem Wert
    evidence_url: str | None = None
    evidence_snippet: str | None = None
    extraction_method: ExtractionMethod = "api"

    @property
    def confidence(self) -> float:
        """Effektive Konfidenz: base + boost - penalty, capped [0,1]."""
        return max(0.0, min(1.0, self.base_confidence + self.boost - self.contradiction_penalty))


class EntityProfile(BaseModel):
    """Pipeline-v2 entity_profile: konsolidiertes Personen-Bild aus allen Claims.

    Vom correlate_node geschrieben, vom reason_node konsumiert. State-only —
    nicht in Postgres persistiert (Scope-Constraint Plan 1e).
    """

    full_name: str
    verification_tier: VerificationTier
    score: int                                  # 0-100, Aggregat
    nor_status: NorStatus
    nor_score: float = 0.0                      # 0.0-1.0, Likelihood des NOR-Falls
    claims_by_type: dict[ClaimType, list[Claim]] = {}
    notes: list[str] = []                       # Hinweise (Wechsel, Contradictions, NOR-Begruendung)


class WebSearchVerification(BaseModel):
    """Llama-Verify-Output (Pipeline-v2 2-Stufen-Suche):

    Stufe A: Llama identifiziert die Person allein aus Snippets, OHNE die
    CRM-Felder Position/Firma als Hinweis zu nutzen — Anti-Confirmation-Bias.
    Stufe B: Llama vergleicht die A-Identifikation mit der CRM-Behauptung.

    Wird vom verify_node geschrieben, vom reason_node + correlate_node
    konsumiert. Claims aus WebSearch entstehen NUR wenn `person_confirmed=True`
    UND `stage_b_match=True`.
    """

    # Aggregat (legacy + abgeleitet)
    person_confirmed: bool = False    # = stage_a_confidence>=0.5 AND stage_b_match
    confidence: float = 0.0           # 0.0-1.0, kombiniert
    role_seen: str | None = None      # Aktuelle Rolle laut Snippets
    company_seen: str | None = None   # Aktuelle Firma laut Snippets
    linkedin_url: str | None = None
    evidence_quotes: list[str] = []
    contradictions: list[str] = []
    note: str = ""

    # Pipeline-v2 2-Stufen-Felder
    stage_a_identity: str | None = None       # Wer ist die Person laut Snippets allein?
    stage_a_confidence: float = 0.0           # 0.0-1.0
    stage_b_match: bool = False               # Passt A zur CRM-Behauptung?
    stage_b_note: str | None = None           # Begruendung Stufe B


class Enrichment(BaseModel):
    """Anreicherung für Reiter 2 (LinkedIn, Wikipedia, etc.)."""

    linkedin_url: str | None = None
    wikipedia_url: str | None = None
    twitter_url: str | None = None
    wikidata_id: str | None = None
    last_press_mention: datetime | None = None
    last_press_title: str | None = None
    last_press_url: str | None = None
    position_now: str | None = None
    company_now: str | None = None
    address_now: str | None = None
    role_change_detected: bool = False
    role_change_note: str | None = None
    # Pipeline-v2 NOR-Reporting (Phase 1e)
    nor_status: NorStatus | None = None
    nor_note: str | None = None
    verification_tier: VerificationTier | None = None
    score: int | None = None                # 0-100, aus EntityProfile


# ──────────────────────────────────────────────────────────────────────────────
# State (LangGraph-TypedDict) — Lookup-Outputs sind Listen, damit parallel-merge
# ──────────────────────────────────────────────────────────────────────────────


def _last(a: list, b: list) -> list:
    """Reducer: nimm den nicht-leeren / neueren Wert."""
    return b if b else a


def _merge_dict(a: dict | None, b: dict | None) -> dict:
    """Reducer: merge zwei dicts."""
    return {**(a or {}), **(b or {})}


def _append_list(a: list | None, b: list | None) -> list:
    """Reducer: concat zwei Listen (für errors)."""
    return list(a or []) + list(b or [])


class CrmCheckState(TypedDict, total=False):
    """Der State pro CRM-Zeile."""

    # Input
    row_idx: int
    raw_row: dict
    salutation_name: str       # "Herr Frank Schwittay"
    name_only: str             # "Frank Schwittay" (raw)
    position: str | None
    company: str | None
    street: str | None
    zip_city: str | None
    country: str | None

    # Normalize-Output
    clean_name: str            # "Frank Schwittay" (stripped)
    last_name: str             # "Schwittay"
    first_name: str            # "Frank"
    matching_key: str          # normalized

    # Lookup-Outputs (Listen damit parallel-merge funktioniert)
    kg_candidates: Annotated[list, _last]
    kg_lobby_candidates: Annotated[list, _last]
    kg_entity_candidates: Annotated[list, _last]
    ni_candidates: Annotated[list, _last]
    ceq_candidates: Annotated[list, _last]
    openregister_candidates: Annotated[list, _last]
    openregister_persons: Annotated[list, _last]
    wikidata_hits: Annotated[list, _last]
    social_profiles: Annotated[list, _last]
    websearch_results: Annotated[list, _last]
    websearch_summary: str
    websearch_verification: WebSearchVerification

    # Pipeline-v2 Phase 1e: Claim-Stream + konsolidiertes Profile
    claims: Annotated[list[Claim], _append_list]   # parallele Lookup-Nodes appenden
    profile: EntityProfile                          # vom correlate_node geschrieben

    # Reasoning-Output
    verdict: RowVerdict
    enrichment: Enrichment

    # Run-Metadata
    errors: Annotated[list[str], _append_list]
    timings_ms: Annotated[dict, _merge_dict]
