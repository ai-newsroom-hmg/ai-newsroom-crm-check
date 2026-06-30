"""Tests fuer PressRelations-Lookup-Node — Pipeline-v2 Tier-2 (READ-ONLY).

Pruefen:
1. SQL ist SELECT-only — kein INSERT/UPDATE/DELETE/COPY (Read-Only-Invariante)
2. _matches_company Suffix-Tolerance (GmbH/AG/SE/KGaA)
3. press_relations_to_claims erzeugt press_mention-Claim mit korrekter Source-Conf
4. Sort-Order: company_match > reach > date

Keine externen Deps.
"""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

from crm_check.graph.claims_mapping import press_relations_to_claims
from crm_check.graph.nodes.press_relations_lookup import (
    PressRelationsHit,
    _matches_company,
    build_query,
    lookup_press_relations,
)
from crm_check.graph.scoring import (
    SOURCE_CONFIDENCE,
    TIER2_SOURCES,
    base_confidence,
)


class TestReadOnlyInvariant:
    """SQL darf NUR SELECT enthalten (User gunterclaude ist in n8n_rw — Schreibrechte
    technisch da, aber NIEMALS nutzen)."""

    def test_sql_is_select_only(self):
        sql, _ = build_query()
        upper = sql.upper()
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "COPY ",
                          "TRUNCATE ", "DROP ", "ALTER ", "CREATE "):
            assert forbidden not in upper, f"Forbidden statement: {forbidden}"
        # Mindestens ein SELECT
        assert "SELECT" in upper

    def test_sql_targets_press_relations_articles(self):
        sql, _ = build_query()
        assert "hypesignals_prod.press_relations_articles" in sql

    def test_sql_uses_fts_index(self):
        # content_tsv ist vorindexiert; phraseto_tsquery('simple', ...) ist das
        # robuste Pattern fuer Eigennamen (deutsche Lemmatisierung schadet hier)
        sql, _ = build_query()
        # NLP-Refactor 2026-06-30: phraseto_tsquery + content_tsv @@ via CTE.
        # Optional zusaetzlich plainto_tsquery fuer Company-Boolean.
        assert "phraseto_tsquery('simple'" in sql
        assert "content_tsv @@" in sql
        assert "'simple'" in sql
        # Bonus: NLP-Features die der intelligenter machen (ts_rank_cd, ts_headline)
        assert "ts_rank_cd" in sql, "Cover-Density-Ranking sollte genutzt werden"
        assert "ts_headline" in sql, "ts_headline liefert bessere Snippets als substring"


class TestSourceWiring:
    """pressrelations muss in scoring.SOURCE_CONFIDENCE + TIER2_SOURCES sein."""

    def test_source_confidence_registered(self):
        assert "pressrelations" in SOURCE_CONFIDENCE
        base, boost = SOURCE_CONFIDENCE["pressrelations"]
        assert base == 0.85  # Pressearchiv-aequivalent
        assert boost == 0.30

    def test_is_tier2(self):
        assert "pressrelations" in TIER2_SOURCES


class TestMatchesCompany:
    def test_exact_token_match(self):
        assert _matches_company("CEO bei ACME beim Treffen", "ACME") is True

    def test_suffix_stripped(self):
        # "ACME GmbH" matched gegen "ACME" im Headline
        assert _matches_company("ACME stellt neuen CEO vor", "ACME GmbH") is True

    def test_no_match(self):
        assert _matches_company("Random text", "ACME") is False

    def test_empty_company(self):
        assert _matches_company("text", "") is False
        assert _matches_company("text", None) is False  # type: ignore[arg-type]

    def test_kgaa_se_handled(self):
        for variant in ("ACME KGaA", "ACME SE", "ACME mbH"):
            assert _matches_company("ACME publishes report", variant) is True


class TestPressRelationsToClaim:
    def test_emits_press_mention_with_correct_source(self):
        hit = PressRelationsHit(
            article_date=date(2026, 6, 1),
            domain="handelsblatt.com",
            url="https://hb.de/x",
            headline="Mueller wird neuer CEO bei ACME",
            sentiment=0.4,
            publication_reach=120000,
            snippet="Mueller wird neuer CEO bei ACME",
            company_match=True,
        )
        claims = press_relations_to_claims(hit)
        assert len(claims) == 1
        c = claims[0]
        assert c.claim_type == "press_mention"
        assert c.source == "pressrelations"
        assert c.base_confidence == base_confidence("pressrelations")
        # company_match → +0.10 boost
        assert c.boost == 0.10
        assert c.evidence_url == "https://hb.de/x"

    def test_empty_headline_no_claim(self):
        hit = PressRelationsHit(headline=None, snippet="text")
        assert press_relations_to_claims(hit) == []

    def test_none_input(self):
        assert press_relations_to_claims(None) == []


class TestLookupSorting:
    """NLP-Refactor 2026-06-30: Sortierung passiert jetzt in SQL (ORDER BY
    company_match_fts DESC, ts_rank_cd DESC, date DESC). Python-Code sortiert
    nicht mehr nach. Test prueft Pass-Through + company_match-Flag-Mapping."""

    def test_company_match_passthrough(self):
        # SQL liefert vorsortiert; Mock simuliert die SQL-Reihenfolge.
        rows = [
            {"article_date": date(2026, 6, 1), "domain": "y.de", "url": "u2",
             "headline": "ACME boss Hans Mueller", "sentiment": 0.5,
             "publication_reach": 50000, "snippet": "ACME boss Hans Mueller",
             "company_match_fts": True},
            {"article_date": date(2026, 1, 1), "domain": "x.de", "url": "u1",
             "headline": "Random news without company", "sentiment": None,
             "publication_reach": 500000, "snippet": "Hans Mueller spoke",
             "company_match_fts": False},
            {"article_date": date(2026, 3, 1), "domain": "z.de", "url": "u3",
             "headline": "Hans Mueller in Brussels", "sentiment": None,
             "publication_reach": 10000, "snippet": "Hans Mueller in Brussels",
             "company_match_fts": False},
        ]
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=rows)
        hits = asyncio.run(lookup_press_relations(
            conn, "Hans Mueller", company="ACME GmbH", days_back=365, limit=3
        ))
        # SQL-Sortierung wird respektiert; company_match-Flag landet im Pydantic-Hit
        assert [h.url for h in hits] == ["u2", "u1", "u3"]
        assert hits[0].company_match is True
        assert hits[1].company_match is False
        assert hits[2].company_match is False

    def test_empty_name_short_circuits(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        hits = asyncio.run(lookup_press_relations(conn, "", company="ACME"))
        assert hits == []
        conn.fetch.assert_not_called()
