"""Tests fuer hugoplus-Lookup-Node — Pipeline-v2 Tier-2 (HB-CMS Agenturen)."""

from __future__ import annotations

from datetime import datetime

from crm_check.graph.claims_mapping import hugoplus_to_claims
from crm_check.graph.nodes.hugoplus_lookup import (
    HugoplusHit,
    _matches_company,
    _strip_html,
    annotate_company_match,
)
from crm_check.graph.scoring import (
    SOURCE_CONFIDENCE,
    TIER2_SOURCES,
    base_confidence,
)


class TestSourceWiring:
    def test_source_confidence_registered(self):
        assert "hugoplus" in SOURCE_CONFIDENCE
        base, boost = SOURCE_CONFIDENCE["hugoplus"]
        assert base == 0.85
        assert boost == 0.30

    def test_is_tier2(self):
        assert "hugoplus" in TIER2_SOURCES


class TestHtmlStrip:
    def test_basic(self):
        assert _strip_html("<p>Hallo <b>Welt</b></p>") == "Hallo Welt"

    def test_entities(self):
        assert _strip_html("AT&amp;T") == "AT&T"

    def test_empty(self):
        assert _strip_html("") == ""
        assert _strip_html(None) == ""  # type: ignore[arg-type]


class TestMatchesCompany:
    def test_strips_suffixes(self):
        assert _matches_company("ACME stellt CEO vor", "ACME GmbH") is True
        assert _matches_company("ACME stellt CEO vor", "ACME AG") is True

    def test_no_match(self):
        assert _matches_company("Wirtschaftsnews ohne Firma", "ACME") is False


class TestAnnotateCompanyMatch:
    def test_sorts_company_match_first(self):
        hits = [
            HugoplusHit(headline="Allgemeine News", source="dpa",
                        article_date=datetime(2026, 6, 1)),
            HugoplusHit(headline="Mueller wird CEO bei ACME", source="Reuters",
                        article_date=datetime(2026, 5, 1)),
        ]
        annot = annotate_company_match(hits, "ACME GmbH")
        assert annot[0].headline.startswith("Mueller")
        assert annot[0].company_match is True


class TestHugoplusToClaims:
    def test_emits_press_mention_correctly(self):
        hit = HugoplusHit(
            doc_id="doc1",
            media_id=12345,
            headline="Mueller wird neuer CEO bei ACME",
            snippet="Mueller wird neuer CEO bei ACME",
            source="Reuters",
            company_match=True,
            article_date=datetime(2026, 6, 1),
        )
        claims = hugoplus_to_claims(hit)
        assert len(claims) == 1
        c = claims[0]
        assert c.claim_type == "press_mention"
        assert c.source == "hugoplus"
        assert c.base_confidence == base_confidence("hugoplus")
        assert c.boost == 0.10  # company_match
        assert c.evidence_url and "12345" in c.evidence_url

    def test_empty_headline_no_claim(self):
        assert hugoplus_to_claims(HugoplusHit(headline="", source="dpa")) == []

    def test_none_input(self):
        assert hugoplus_to_claims(None) == []
