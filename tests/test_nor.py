"""Tests fuer NOR-Discovery — Pipeline-v2 Z.72-82.

A ✓ + B ✓ → public (Person identifiziert, Verbindung oeffentlich)
A ✓ + B ✗ → nor    (Person identifiziert, Verbindung NICHT oeffentlich — Scoop)
A ✗      → unidentified

Keine externen Deps.
"""

from __future__ import annotations

import asyncio

from crm_check.graph.nodes.correlate_node import _compute_nor, make_correlate_node
from crm_check.graph.state import Claim, CrmCheckState, WebSearchVerification


def _identity(source: str, conf: float = 0.80) -> Claim:
    """Tier-1-Identifikations-Claim mit gewuenschter Konfidenz."""
    return Claim(
        claim_type="person_identity",
        value="Hans Mueller",
        source=source,  # type: ignore[arg-type]
        base_confidence=conf,
    )


def _press_mention() -> Claim:
    return Claim(
        claim_type="press_mention",
        value="Mueller spricht in Davos",
        source="ni_mentions",
        base_confidence=0.85,
    )


class TestComputeNor:
    """Direkte Logik-Tests fuer _compute_nor() — kein State-Loop."""

    def test_a_tier1_plus_b_press_is_public(self):
        claims = {
            "person_identity": [_identity("kg_lobby_persons", 0.80)],
            "press_mention": [_press_mention()],
        }
        status, score, note = _compute_nor(claims, verification_person_confirmed=False)
        assert status == "public"
        assert "oeffentliche Erwaehnung" in note

    def test_a_tier1_plus_b_llm_verified_is_public(self):
        # LLM-bestaetigte WebSearch zaehlt als B-Signal (auch ohne Press-Mention)
        claims = {"person_identity": [_identity("ceq_api", 0.85)]}
        status, _, _ = _compute_nor(claims, verification_person_confirmed=True)
        assert status == "public"

    def test_a_tier1_no_b_is_nor(self):
        # Person identifiziert, aber keine Presse-Mention zur CRM-Behauptung
        claims = {"person_identity": [_identity("openregister", 0.75)]}
        status, score, note = _compute_nor(claims, verification_person_confirmed=False)
        assert status == "nor"
        assert score == 0.75  # NOR-Score = beste Tier-1-Konfidenz
        assert "amtlich identifiziert" in note

    def test_a_tier1_weak_below_threshold_is_unidentified(self):
        # Tier-1-Quelle ist da, aber Konfidenz < 0.70 → kein A-Signal
        claims = {"person_identity": [_identity("wikidata", 0.50)]}
        status, _, _ = _compute_nor(claims, verification_person_confirmed=False)
        assert status == "unidentified"

    def test_no_tier1_identity_is_unidentified(self):
        # Nur Tier-2/3-Quellen (perplexity, ni_entities) → A-Fehler
        c = Claim(
            claim_type="person_identity", value="Hans",
            source="perplexity", base_confidence=0.70,
        )
        status, _, _ = _compute_nor({"person_identity": [c]}, verification_person_confirmed=False)
        assert status == "unidentified"

    def test_empty_claims_is_unidentified(self):
        status, _, _ = _compute_nor({}, verification_person_confirmed=False)
        assert status == "unidentified"


class TestNorViaCorrelateNode:
    """Voll-Loop durch correlate_node — pruefen dass NOR korrekt im profile landet."""

    def test_nor_case_yields_correct_profile(self):
        # Mittelstand-CEO: nur in Handelsregister, keine Presse
        identity = _identity("openregister", 0.75)
        employer = Claim(
            claim_type="current_employer", value="Mittelstand AG",
            source="openregister", base_confidence=0.50, boost=0.15,
        )
        state: CrmCheckState = {
            "clean_name": "Hans Mueller",
            "position": "Geschaeftsfuehrer",
            "company": "Mittelstand AG",
            "claims": [identity, employer],
        }
        result = asyncio.run(make_correlate_node()(state))
        profile = result["profile"]
        assert profile.nor_status == "nor"
        assert profile.nor_score > 0
        # NOR-Begruendung sollte in notes[0] stehen
        assert any("amtlich identifiziert" in n for n in profile.notes)

    def test_public_case_with_verification(self):
        identity = _identity("ceq_api", 0.85)
        state: CrmCheckState = {
            "clean_name": "Hans Mueller",
            "claims": [identity],
            "websearch_verification": WebSearchVerification(
                person_confirmed=True,
                confidence=0.85,
                stage_a_identity="CEO bei ACME",
                stage_a_confidence=0.85,
                stage_b_match=True,
            ),
        }
        result = asyncio.run(make_correlate_node()(state))
        assert result["profile"].nor_status == "public"

    def test_unidentified_when_no_tier1(self):
        state: CrmCheckState = {
            "clean_name": "Unbekannt",
            "claims": [],
        }
        result = asyncio.run(make_correlate_node()(state))
        assert result["profile"].nor_status == "unidentified"
        assert result["profile"].verification_tier == "unconfirmed"
