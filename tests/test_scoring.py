"""Tests fuer crm_check.graph.scoring — Konfidenz-Hierarchie + Tier-Schwellen.

Direkte Validierung der Pipeline-v2-Konstanten (Z.84-95 + Z.174-179).
Keine externen Deps.
"""

from __future__ import annotations

import pytest

from crm_check.graph.scoring import (
    CONFIRMED_MIN,
    PROBABLE_MIN,
    SOURCE_CONFIDENCE,
    TIER1_SOURCES,
    TIER2_SOURCES,
    base_confidence,
    max_boost,
    score_from_confidence,
    tier_for_score,
)


class TestTierThresholds:
    """3-Tier-Verifikation: Confirmed ≥80 / Probable 40-79 / Unconfirmed <40."""

    @pytest.mark.parametrize(
        "score,expected",
        [
            (100, "confirmed"),
            (80, "confirmed"),
            (79, "probable"),
            (50, "probable"),
            (40, "probable"),
            (39, "unconfirmed"),
            (0, "unconfirmed"),
        ],
    )
    def test_tier_for_score(self, score, expected):
        assert tier_for_score(score) == expected

    def test_thresholds_match_spec(self):
        # Pipeline v2 Z.174: Confirmed ≥80, Probable 40-79, Unconfirmed <40
        assert CONFIRMED_MIN == 80
        assert PROBABLE_MIN == 40


class TestSourceConfidence:
    """Pipeline v2 Z.84-95 — base + max_boost pro Quelle."""

    def test_tier1_strong_sources(self):
        # Pressearchiv-Aequivalent / autoritative Register
        assert base_confidence("kg_person_universe") == 0.85
        assert base_confidence("ni_mentions") == 0.85
        # CEQ entfernt 2026-06-29

    def test_tier1_register_sources(self):
        assert base_confidence("wikidata") == 0.70
        assert base_confidence("kg_lobby_persons") == 0.70

    def test_handelsregister_has_max_boost(self):
        # Handelsregister: 0.50 + bis 0.30 Boost (Adress-/Org-Abgleich)
        assert base_confidence("openregister") == 0.50
        assert max_boost("openregister") == 0.30

    def test_websearch_capped_no_boost(self):
        # SearXNG: 0.70 ohne Boost — Web-Aggregator braucht Korroboration
        assert base_confidence("perplexity") == 0.70
        assert max_boost("perplexity") == 0.00

    def test_llm_reasoning_is_helper(self):
        # llm_reasoning ist NIE Primaerquelle
        assert base_confidence("llm_reasoning") == 0.00
        assert max_boost("llm_reasoning") == 0.00

    def test_unknown_source_returns_zero(self):
        assert base_confidence("nonexistent_source") == 0.0  # type: ignore[arg-type]
        assert max_boost("nonexistent_source") == 0.0  # type: ignore[arg-type]


class TestTierBuckets:
    """NOR-Discovery braucht klare Tier-1/Tier-2-Mengen."""

    def test_tier1_contains_official_registers(self):
        for s in ("kg_person_universe", "kg_lobby_persons",
                  "openregister", "wikidata"):
            assert s in TIER1_SOURCES, f"{s} fehlt in TIER1_SOURCES"

    def test_tier1_excludes_press(self):
        # ni_mentions ist Pressequelle (Tier 2), nicht Identifikations-Register
        assert "ni_mentions" not in TIER1_SOURCES

    def test_tier2_contains_press_and_web(self):
        assert "ni_mentions" in TIER2_SOURCES
        assert "perplexity" in TIER2_SOURCES

    def test_tier1_and_tier2_disjoint(self):
        assert not (TIER1_SOURCES & TIER2_SOURCES)


class TestScoreFromConfidence:
    @pytest.mark.parametrize(
        "conf,expected",
        [
            (0.0, 0),
            (0.5, 50),
            (0.85, 85),
            (1.0, 100),
            (1.5, 100),    # cap
            (-0.2, 0),     # floor
        ],
    )
    def test_mapping(self, conf, expected):
        assert score_from_confidence(conf) == expected


class TestSourceConfidenceTableCompleteness:
    """Schluessel-Konsistenz — verhindert dass beim Erweitern eine Source vergisst wird."""

    def test_all_sources_have_2_tuple(self):
        for source, value in SOURCE_CONFIDENCE.items():
            assert isinstance(value, tuple), f"{source}: expected tuple"
            assert len(value) == 2, f"{source}: expected (base, max_boost)"
            base, boost = value
            assert 0.0 <= base <= 1.0, f"{source}: base out of range"
            assert 0.0 <= boost <= 1.0, f"{source}: max_boost out of range"
