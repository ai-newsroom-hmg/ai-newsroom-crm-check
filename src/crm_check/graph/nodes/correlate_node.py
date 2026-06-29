"""Correlate-Node — Pipeline-v2-Stufe 3 (CORRELATE + MERGE).

Konsumiert den `state.claims`-Stream aller parallelen Lookup-Nodes und macht
drei Dinge:

1. **Gruppieren** nach `claim_type`. Innerhalb der Gruppe Werte normalisieren
   (lower, strip, simple-fuzzy fuer Firmen-Suffixe).
2. **Korroborations-Matrix:** Mehrere unabhaengige Quellen mit dem gleichen
   normalisierten Wert → Boost auf dem fuehrenden Claim, `corroborated_by[]`
   wird gefuellt. Cap auf `max_boost(source)`.
3. **Contradictions:** Andere Werte fuer den gleichen `claim_type` →
   `contradicted_by[]` + Penalty. Bei `current_position`/`current_employer`
   wird ausserdem gegen den CRM-Wert verglichen — Mismatch = Penalty.

Output:
- `state.profile: EntityProfile` mit Score (0-100), verification_tier,
  nor_status, claims_by_type.
- NOR-Logik (Pipeline-v2 Z.72-82):
    * A = ≥1 Tier-1-Quelle mit `person_identity`-Claim
    * B = ≥1 Press-Mention ODER verify.person_confirmed=True
    * A∧B = public  / A∧¬B = nor  / ¬A = unidentified
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable

from crm_check.graph.match_gate import passes_identity_gate
from crm_check.graph.scoring import (
    TIER1_SOURCES,
    score_from_confidence,
    tier_for_score,
)
from crm_check.graph.state import (
    Claim,
    ClaimType,
    CrmCheckState,
    EntityProfile,
    NorStatus,
    SourceName,
)

log = logging.getLogger(__name__)

NodeFn = Callable[[CrmCheckState], Awaitable[CrmCheckState]]


# Suffixe die beim Firmen-Vergleich gestrippt werden (lokal — keine externe Lib)
_ORG_SUFFIXES = re.compile(
    r"\b(gmbh|ag|kgaa|kg|se|ohg|gbr|e\.v\.|ev|mbh|ug|co\.|& co\.?|holding|group|gruppe)\b",
    re.IGNORECASE,
)


def _normalize_value(claim_type: ClaimType, value: str) -> str:
    """Locker-fuzzy Vergleich pro Typ. Identitaet: Last-Name only;
    Position: lowercase; Employer: strip suffixes; URL: lowercase + trailing-/."""
    v = (value or "").strip()
    if not v:
        return ""
    if claim_type == "person_identity":
        # Last token als Vergleichsanker (Vorname-Variationen tolerieren)
        parts = v.split()
        return parts[-1].casefold() if parts else v.casefold()
    if claim_type == "current_employer":
        cleaned = _ORG_SUFFIXES.sub("", v).strip().casefold()
        return re.sub(r"\s+", " ", cleaned)
    if claim_type == "linkedin_url":
        return v.rstrip("/").casefold()
    return v.casefold()


def _group_by_normalized(claims: list[Claim]) -> dict[str, list[Claim]]:
    """Gruppiert eine Claim-Liste (gleicher claim_type!) nach normalisiertem Wert."""
    out: dict[str, list[Claim]] = {}
    for c in claims:
        key = _normalize_value(c.claim_type, c.value)
        if not key:
            continue
        out.setdefault(key, []).append(c)
    return out


def _check_crm_match(claim_type: ClaimType, claim_value: str, crm_value: str | None) -> bool:
    """Stimmt der Claim mit der CRM-Behauptung ueberein? Liberal fuer Employer."""
    if not crm_value:
        return False
    return _normalize_value(claim_type, claim_value) == _normalize_value(claim_type, crm_value)


def _apply_corroboration(grouped: dict[str, list[Claim]]) -> list[Claim]:
    """Pro Wert-Gruppe: setzt corroborated_by[] auf den staerksten Claim,
    erhoeht boost um 0.05 pro zusaetzlicher unabhaengiger Quelle (cap durch max_boost)."""
    leaders: list[Claim] = []
    for _key, group in grouped.items():
        if not group:
            continue
        # Sortiere nach effektiver Konfidenz, beste Quelle zuerst
        group.sort(key=lambda c: c.confidence, reverse=True)
        leader = group[0]
        unique_sources = {c.source for c in group}
        if len(unique_sources) > 1:
            # Korroborations-Boost: +0.05 pro zusaetzlicher Quelle
            extra = 0.05 * (len(unique_sources) - 1)
            leader = leader.model_copy(update={
                "boost": leader.boost + extra,
                "corroborated_by": [s for s in unique_sources if s != leader.source],
            })
        leaders.append(leader)
    return leaders


def _apply_contradictions(
    leaders_by_type: dict[ClaimType, list[Claim]],
    crm_position: str | None,
    crm_company: str | None,
) -> dict[ClaimType, list[Claim]]:
    """Penalty fuer abweichende Werte innerhalb des gleichen claim_type
    + Mismatch gegen CRM-Behauptung."""
    out: dict[ClaimType, list[Claim]] = {}
    for ctype, leaders in leaders_by_type.items():
        if not leaders:
            out[ctype] = leaders
            continue
        leaders = sorted(leaders, key=lambda c: c.confidence, reverse=True)
        # Wenn mehrere unterschiedliche Werte (= mehrere Leaders nach Gruppierung):
        # Penalty fuer alle ausser dem fuehrenden
        if len(leaders) > 1:
            top = leaders[0]
            others = leaders[1:]
            top = top.model_copy(update={
                "contradicted_by": [c.source for c in others],
                "contradiction_penalty": top.contradiction_penalty + 0.05 * len(others),
            })
            leaders = [top, *others]
        # CRM-Mismatch fuer Position/Employer
        crm_value = crm_position if ctype == "current_position" else crm_company if ctype == "current_employer" else None
        if crm_value:
            new_leaders: list[Claim] = []
            for c in leaders:
                if not _check_crm_match(ctype, c.value, crm_value):
                    new_leaders.append(c.model_copy(update={
                        "contradiction_penalty": c.contradiction_penalty + 0.10,
                    }))
                else:
                    new_leaders.append(c)
            leaders = new_leaders
        out[ctype] = leaders
    return out


def _compute_nor(
    claims_by_type: dict[ClaimType, list[Claim]],
    verification_person_confirmed: bool,
) -> tuple[NorStatus, float, str]:
    """A=Tier-1-Identifier, B=Presse oder LLM-bestaetigt → NOR-Triage."""
    identity_claims = claims_by_type.get("person_identity", [])
    has_tier1_identity = any(
        c.source in TIER1_SOURCES and c.confidence >= 0.70
        for c in identity_claims
    )
    has_press = bool(claims_by_type.get("press_mention", []))
    b_signal = has_press or verification_person_confirmed

    if has_tier1_identity and b_signal:
        return "public", 0.0, "Person identifiziert + oeffentliche Erwaehnung."
    if has_tier1_identity and not b_signal:
        # NOR-Score: Likelihood dass die Abwesenheit von Presse-Mentions echte
        # NOR-Investigativ-Signal ist. Heuristik: je hoeher die Tier-1-Konfidenz,
        # desto stuetzender die "unexplained absence".
        best = max((c.confidence for c in identity_claims if c.source in TIER1_SOURCES), default=0.0)
        return "nor", round(best, 2), "Person amtlich identifiziert, aber keine Presse-Mention zur Position/Firma."
    return "unidentified", 0.0, "Person konnte in autoritativen Registern nicht identifiziert werden."


def _aggregate_score(claims_by_type: dict[ClaimType, list[Claim]]) -> int:
    """Score = gewichteter Mittelwert der besten Claim-Confidence pro relevanten Typ."""
    weights: dict[ClaimType, float] = {
        "person_identity":  0.40,
        "current_position": 0.25,
        "current_employer": 0.25,
        "press_mention":    0.10,
        # linkedin_url / address fliessen nicht in Score (Anreicherung, nicht Verdict)
    }
    total_w = 0.0
    total_c = 0.0
    for ctype, w in weights.items():
        leaders = claims_by_type.get(ctype, [])
        if not leaders:
            continue
        best = max(c.confidence for c in leaders)
        total_w += w
        total_c += w * best
    if total_w <= 0.0:
        return 0
    return score_from_confidence(total_c / total_w)


def make_correlate_node() -> NodeFn:
    """Erzeugt den Correlate-Node — sitzt nach verify, vor reason."""

    async def node(state: CrmCheckState) -> CrmCheckState:
        t0 = time.monotonic()
        claims: list[Claim] = state.get("claims") or []
        if not claims:
            # Kein Identifikations-Signal — leeres Profil
            profile = EntityProfile(
                full_name=state.get("clean_name", "") or "",
                verification_tier="unconfirmed",
                score=0,
                nor_status="unidentified",
                nor_score=0.0,
                claims_by_type={},
                notes=["Keine Claims aus Lookup-Quellen."],
            )
            return CrmCheckState(profile=profile, timings_ms={"correlate": _ms(t0)},
                                 match_gate_decisions=[])

        # 0. Identity-Match-Gate (Pipeline-v2 Phase 1g):
        # B6-Vault-Regel: NIEMALS person_identity-Claim nur auf Last-Name.
        # Vornamens-/Gender-/Firma-Anker pflicht. Reject-Claims werden NICHT
        # weitergereicht (kein Surface-Match-False-Positive wie Ulrike vs Ulrich
        # Pieper 2026-06-29). Audit-Trail in state.match_gate_decisions.
        crm_first = state.get("first_name", "") or ""
        crm_last = state.get("last_name", "") or ""
        crm_company = state.get("company")
        gate_log: list[dict] = []
        accepted_claims: list[Claim] = []
        for c in claims:
            if c.claim_type != "person_identity":
                accepted_claims.append(c)
                continue
            # value ist "Vorname Nachname" — splitten für Source-Side
            parts = (c.value or "").split()
            src_first = parts[0] if len(parts) >= 2 else None
            src_last = parts[-1] if parts else ""
            # Source-Org-Hint aus evidence_snippet (best-effort) ist nicht
            # robust → wir geben None und verlassen uns auf R5 (firstname_match).
            decision = passes_identity_gate(
                crm_first=crm_first,
                crm_last=crm_last,
                crm_company=crm_company,
                src_first=src_first,
                src_last=src_last,
                src_org=None,
            )
            gate_log.append({
                "source": c.source,
                "claim_value": c.value,
                "accepted": decision.accepted,
                "rule": decision.rule,
                "reason": decision.reason,
                "evidence_url": c.evidence_url,
            })
            if decision.accepted:
                accepted_claims.append(c)
            else:
                log.info(
                    "match_gate REJECT row=%s source=%s rule=%s value=%r",
                    state.get("row_idx"), c.source, decision.rule, c.value,
                )
        claims = accepted_claims

        # 1. Gruppieren by claim_type
        by_type: dict[ClaimType, list[Claim]] = {}
        for c in claims:
            by_type.setdefault(c.claim_type, []).append(c)

        # 2. Korroborations-Boosts pro Typ
        leaders_by_type: dict[ClaimType, list[Claim]] = {}
        for ctype, group in by_type.items():
            grouped = _group_by_normalized(group)
            leaders_by_type[ctype] = _apply_corroboration(grouped)

        # 3. Contradictions + CRM-Mismatch
        leaders_by_type = _apply_contradictions(
            leaders_by_type,
            crm_position=state.get("position"),
            crm_company=state.get("company"),
        )

        # 4. Score + Tier
        score = _aggregate_score(leaders_by_type)
        tier = tier_for_score(score)

        # 5. NOR
        verification = state.get("websearch_verification")
        person_confirmed = bool(verification and verification.person_confirmed)
        nor_status, nor_score, nor_note = _compute_nor(leaders_by_type, person_confirmed)

        # 6. Notes (Contradictions-Hinweise)
        notes: list[str] = [nor_note]
        for ctype in ("current_position", "current_employer"):
            leaders = leaders_by_type.get(ctype, [])  # type: ignore[arg-type]
            for c in leaders:
                if c.contradicted_by:
                    notes.append(
                        f"{ctype}: '{c.value}' (Konsens) — Abweichungen aus {','.join(c.contradicted_by)}"
                    )

        profile = EntityProfile(
            full_name=state.get("clean_name", "") or "",
            verification_tier=tier,
            score=score,
            nor_status=nor_status,
            nor_score=nor_score,
            claims_by_type=leaders_by_type,
            notes=notes,
        )
        return CrmCheckState(
            profile=profile,
            timings_ms={"correlate": _ms(t0)},
            match_gate_decisions=gate_log,
        )

    return node


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
