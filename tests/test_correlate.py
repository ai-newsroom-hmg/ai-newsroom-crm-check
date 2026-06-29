"""Tests fuer crm_check.graph.nodes.correlate_node — Pipeline-v2 CORRELATE+MERGE.

Synthetische Claim-Streams, prueft Korroboration-Boost + Contradiction-Penalty
+ Score-Aggregat + Tier-Zuweisung. Keine externen Deps.
"""

from __future__ import annotations

import asyncio

import pytest

from crm_check.graph.nodes.correlate_node import (
    _aggregate_score,
    _apply_contradictions,
    _apply_corroboration,
    _group_by_normalized,
    _normalize_value,
    make_correlate_node,
)
from crm_check.graph.state import Claim, CrmCheckState


def _claim(
    *,
    ctype="person_identity",
    value="Hans Mueller",
    source="kg_lobby_persons",
    base=0.70,
    boost=0.0,
) -> Claim:
    return Claim(
        claim_type=ctype, value=value, source=source,
        base_confidence=base, boost=boost,
    )


class TestNormalizeValue:
    def test_person_identity_uses_lastname(self):
        # Anti-Confirmation-Bias: Vorname-Variationen sollen nicht doppeln
        assert _normalize_value("person_identity", "Hans Mueller") == "mueller"
        assert _normalize_value("person_identity", "H. Mueller") == "mueller"

    def test_employer_strips_suffixes(self):
        # "ACME GmbH" und "ACME AG" sollen NICHT zu gleichem Key kollabieren —
        # AG/GmbH/SE/KGaA werden gestrippt, der Kern muss matchen
        a = _normalize_value("current_employer", "ACME GmbH")
        b = _normalize_value("current_employer", "ACME")
        assert a == b

    def test_linkedin_strips_trailing_slash(self):
        a = _normalize_value("linkedin_url", "https://linkedin.com/in/hans/")
        b = _normalize_value("linkedin_url", "https://linkedin.com/in/hans")
        assert a == b


class TestCorroborationBoost:
    def test_two_sources_same_value_adds_boost(self):
        # 2 Quellen sagen Hans Mueller → Korroboration auf den staerksten Claim
        c1 = _claim(source="kg_lobby_persons", base=0.70)
        c2 = _claim(source="wikidata", base=0.70)
        grouped = _group_by_normalized([c1, c2])
        leaders = _apply_corroboration(grouped)
        assert len(leaders) == 1
        leader = leaders[0]
        # +0.05 fuer eine zusaetzliche unabhaengige Quelle
        assert leader.boost == pytest.approx(0.05)
        assert "wikidata" in leader.corroborated_by or "kg_lobby_persons" in leader.corroborated_by

    def test_three_sources_stack_boost(self):
        c1 = _claim(source="kg_lobby_persons", base=0.70)
        c2 = _claim(source="wikidata", base=0.70)
        c3 = _claim(source="kg_person_universe", base=0.85)
        leaders = _apply_corroboration(_group_by_normalized([c1, c2, c3]))
        assert len(leaders) == 1
        # +0.05 * 2 zusaetzliche Quellen
        assert leaders[0].boost == pytest.approx(0.10)

    def test_same_source_twice_no_double_boost(self):
        # Korroboration zaehlt nur unabhaengige Quellen
        c1 = _claim(source="kg_lobby_persons", base=0.70)
        c2 = _claim(source="kg_lobby_persons", base=0.70)
        leaders = _apply_corroboration(_group_by_normalized([c1, c2]))
        # 1 Source-Klasse → kein Boost
        assert leaders[0].boost == 0.0


class TestContradictions:
    def test_crm_position_mismatch_adds_penalty(self):
        # Claim sagt "Vorstand", CRM sagt "Marketing-Leiter" → Penalty
        claim = _claim(ctype="current_position", value="Vorstand",
                       source="kg_person_universe", base=0.85)
        result = _apply_contradictions(
            {"current_position": [claim]},
            crm_position="Marketing-Leiter",
            crm_company=None,
        )
        assert result["current_position"][0].contradiction_penalty >= 0.10

    def test_crm_employer_match_no_penalty(self):
        # ACME GmbH passt zu ACME → kein Penalty trotz Suffix-Unterschied
        claim = _claim(ctype="current_employer", value="ACME GmbH",
                       source="kg_person_universe", base=0.85)
        result = _apply_contradictions(
            {"current_employer": [claim]},
            crm_position=None,
            crm_company="ACME",
        )
        assert result["current_employer"][0].contradiction_penalty == 0.0

    def test_multiple_different_positions_get_penalty_on_loser(self):
        # Zwei verschiedene Positions (nach group_by waeren das zwei Leaders)
        c1 = _claim(ctype="current_position", value="CEO",
                    source="kg_person_universe", base=0.85, boost=0.10)  # winner
        c2 = _claim(ctype="current_position", value="CFO",
                    source="wikidata", base=0.70)  # loser
        result = _apply_contradictions(
            {"current_position": [c1, c2]},
            crm_position=None, crm_company=None,
        )
        # Winner kennzeichnet Loser-Source als Contradictor
        winner = result["current_position"][0]
        assert winner.value == "CEO"
        assert "wikidata" in winner.contradicted_by
        assert winner.contradiction_penalty > 0


class TestScoreAggregate:
    def test_empty_returns_zero(self):
        assert _aggregate_score({}) == 0

    def test_only_identity_gives_partial_score(self):
        # Nur person_identity Gewicht 0.40, also max 0.40 * conf = 40 fuer base=1.0
        claim = _claim(value="Hans", source="kg_person_universe", base=1.0)
        score = _aggregate_score({"person_identity": [claim]})
        # 0.40 / 0.40 weight = 1.0 conf → 100 score (weil weights nur fuer
        # vorhandene Typen summiert werden)
        assert score == 100

    def test_full_profile_scores_high(self):
        identity = _claim(value="Hans Mueller", source="kg_person_universe", base=0.85, boost=0.10)
        position = _claim(ctype="current_position", value="CEO", source="kg_person_universe", base=0.85, boost=0.10)
        employer = _claim(ctype="current_employer", value="ACME", source="kg_person_universe", base=0.85, boost=0.10)
        score = _aggregate_score({
            "person_identity": [identity],
            "current_position": [position],
            "current_employer": [employer],
        })
        # Alle drei bei conf=0.95 → ~95 Score
        assert score >= 90

    def test_weak_only_unidentified(self):
        # SearXNG alleine ohne Tier-1
        identity = _claim(value="Hans", source="perplexity", base=0.30, boost=0.0)
        score = _aggregate_score({"person_identity": [identity]})
        # 0.30 conf → 30 Score → unconfirmed
        assert score < 40


class TestCorrelateNodeIntegration:
    """Voll-Loop durch make_correlate_node()."""

    def test_no_claims_yields_unidentified(self):
        state: CrmCheckState = {"clean_name": "Test"}
        node = make_correlate_node()
        result = asyncio.run(node(state))
        profile = result["profile"]
        assert profile.verification_tier == "unconfirmed"
        assert profile.nor_status == "unidentified"
        assert profile.score == 0

    def test_strong_claims_yield_confirmed_public(self):
        # 3 Tier-1-Quellen sagen alle das gleiche, plus press_mention → public
        identity_a = _claim(value="Hans Mueller", source="kg_lobby_persons", base=0.70)
        identity_b = _claim(value="Hans Mueller", source="kg_person_universe", base=0.85)
        identity_c = _claim(value="Hans Mueller", source="wikidata", base=0.70)
        position = _claim(ctype="current_position", value="CEO", source="kg_person_universe", base=0.85)
        employer = _claim(ctype="current_employer", value="ACME", source="kg_person_universe", base=0.85)
        press = _claim(ctype="press_mention", value="Mueller spricht in Davos",
                       source="ni_mentions", base=0.85)
        state: CrmCheckState = {
            "clean_name": "Hans Mueller", "first_name": "Hans", "last_name": "Mueller",
            "position": "CEO",
            "company": "ACME",
            "claims": [identity_a, identity_b, identity_c, position, employer, press],
        }
        result = asyncio.run(make_correlate_node()(state))
        profile = result["profile"]
        assert profile.nor_status == "public"
        assert profile.verification_tier == "confirmed"
        assert profile.score >= 80

    def test_crm_position_mismatch_penalty_drops_score(self):
        # Quelle sagt CFO, CRM sagt Vorstand → Score sinkt durch Penalty
        identity = _claim(value="Hans Mueller", source="kg_person_universe", base=0.85)
        position = _claim(ctype="current_position", value="CFO",
                          source="kg_person_universe", base=0.85)
        state: CrmCheckState = {
            "clean_name": "Hans Mueller", "first_name": "Hans", "last_name": "Mueller",
            "position": "Vorstand",
            "company": None,
            "claims": [identity, position],
        }
        result = asyncio.run(make_correlate_node()(state))
        profile = result["profile"]
        position_leaders = profile.claims_by_type["current_position"]
        assert position_leaders[0].contradiction_penalty >= 0.10
