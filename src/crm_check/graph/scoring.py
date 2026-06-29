"""Pipeline-v2 Konfidenz-Hierarchie + 3-Tier-Verifikations-Schwellen.

Werte direkt aus `Konzepte/AI Newsroom/Entity Intelligence - Pipeline v2.md`
(Z.84-95 base + Boost-Conditions; Z.174-179 Tier-Schwellen). Eine zentrale
Konstanten-Tabelle statt verstreuter Hardcodes in reason_node.
"""

from __future__ import annotations

from crm_check.graph.state import SourceName, VerificationTier

# (base_confidence, max_boost) pro Quelle. effektive Konfidenz wird im
# correlate_node als base + verliehenem_boost - contradiction_penalty berechnet
# und auf [0, 1] gecappt.
SOURCE_CONFIDENCE: dict[SourceName, tuple[float, float]] = {
    # Tier 1 — autoritative Register (Pipeline-v2 Tabelle Z.84-95)
    "kg_person_universe": (0.85, 0.30),   # Pressearchiv-aequivalent (Journalisten-verifiziert)
    "kg_lobby_persons":   (0.70, 0.25),   # Amtliches Lobbyregister
    "kg_entities":        (0.60, 0.15),   # Organisations-Trigram, schwaecher
    "openregister":       (0.50, 0.30),   # Handelsregister: Adress-/Org-Abgleich = boost
    "wikidata":           (0.70, 0.25),   # Autoritative Register-Klasse
    # Tier 2 — Presse
    "ni_mentions":        (0.85, 0.30),   # Pressearchiv (Nicht-Genios)
    "ni_entities":        (0.60, 0.20),
    "pressrelations":     (0.85, 0.30),   # wraite hypesignals_prod (59,7M Artikel, FTS) — READ-ONLY
    # Tier 3 — Social/Web
    "perplexity":         (0.70, 0.00),   # SearXNG/Web-Aggregator
    "linkedin":           (0.65, 0.00),   # Social, braucht Korroboration
    # Hilfsklasse — NIE Primaerquelle
    "llm_reasoning":      (0.00, 0.00),
}

# Tier-1-Quellen fuer NOR-Discovery (A-Identifikation laut Pipeline v2)
TIER1_SOURCES: frozenset[SourceName] = frozenset({
    "kg_person_universe",
    "kg_lobby_persons",
    "openregister",
    "wikidata",
})

# Tier-2-Quellen fuer Stufe-B (Assoziations-Check)
TIER2_SOURCES: frozenset[SourceName] = frozenset({
    "ni_mentions",
    "ni_entities",
    "pressrelations",
    "perplexity",
})

# 3-Tier-Schwellen (Pipeline-v2 Z.174)
CONFIRMED_MIN = 80
PROBABLE_MIN = 40


def base_confidence(source: SourceName) -> float:
    """Lookup base_confidence — defaults to 0.0 fuer unbekannte Quellen."""
    return SOURCE_CONFIDENCE.get(source, (0.0, 0.0))[0]


def max_boost(source: SourceName) -> float:
    """Lookup max_boost — defaults to 0.0."""
    return SOURCE_CONFIDENCE.get(source, (0.0, 0.0))[1]


def tier_for_score(score: int) -> VerificationTier:
    """Pipeline-v2 3-Tier: Confirmed >=80 / Probable 40-79 / Unconfirmed <40."""
    if score >= CONFIRMED_MIN:
        return "confirmed"
    if score >= PROBABLE_MIN:
        return "probable"
    return "unconfirmed"


def score_from_confidence(confidence: float) -> int:
    """Map effektive Konfidenz [0,1] auf Score [0,100]."""
    return int(round(max(0.0, min(1.0, confidence)) * 100))
