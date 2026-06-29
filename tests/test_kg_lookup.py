"""KG-Lookup-Tests — Unit-Logic (SQL + Ranking). Integration via marker."""

from datetime import date

import pytest

from crm_check.graph.nodes.kg_lookup import KgCandidate, build_query, rank_with_company


def _cand(**kwargs) -> KgCandidate:
    """Convenience-Factory mit Defaults."""
    base = dict(
        person_id=1,
        wikidata_id=None,
        full_name="Test",
        normalized_full_name="test",
        role=None,
        primary_org=None,
        company_id=None,
        linkedin_url=None,
        last_seen=date(2026, 1, 1),
        is_active=True,
        is_stale_linkedin=False,
        is_stale_wikidata=False,
        is_stale_ceq=False,
        similarity_score=0.5,
        company_match=False,
    )
    base.update(kwargs)
    return KgCandidate(**base)


class TestBuildQuery:
    def test_uses_trigram_operator(self):
        sql, _ = build_query()
        # pg_trgm Operator ist `%` (single percent) — der einzige Weg den
        # GIN-Trigram-Index zu treffen
        assert "normalized_full_name % $1" in sql

    def test_orders_by_similarity_desc(self):
        sql, _ = build_query()
        assert "ORDER BY similarity(normalized_full_name, $1) DESC" in sql

    def test_returns_staleness_flags(self):
        sql, _ = build_query()
        # GENERATED-Flags müssen mit selected werden — sonst verlieren wir
        # den ganzen Sinn von kg.person_universe für CRM-Check
        for col in ("is_stale_linkedin", "is_stale_wikidata", "is_stale_ceq"):
            assert col in sql

    def test_limit_param(self):
        sql, limit = build_query(limit=3)
        assert "LIMIT $2" in sql
        assert limit == 3


class TestRankWithCompany:
    def test_company_match_promotes_lower_similarity(self):
        # Zwei Treffer, A hat höhere Trigram-Similarity ohne Firma-Match,
        # B niedrigere mit Firma-Match → B muss vor A landen
        cands = [
            _cand(person_id=1, full_name="Frank Schwittay", similarity_score=0.95,
                  primary_org="Anderer Konzern AG"),
            _cand(person_id=2, full_name="Frank Schwittay", similarity_score=0.85,
                  primary_org="Trend Micro Deutschland GmbH"),
        ]
        ranked = rank_with_company(cands, "Trend Micro Deutschland GmbH")
        assert ranked[0].person_id == 2
        assert ranked[0].company_match is True
        assert ranked[1].company_match is False

    def test_substring_match_short_in_long(self):
        # Excel-Firma "Trend Micro", KG-`primary_org` "Trend Micro Deutschland GmbH"
        cands = [_cand(primary_org="Trend Micro Deutschland GmbH")]
        ranked = rank_with_company(cands, "Trend Micro")
        assert ranked[0].company_match is True

    def test_substring_match_long_contains_short(self):
        cands = [_cand(primary_org="McKinsey")]
        ranked = rank_with_company(cands, "McKinsey & Company, Inc.")
        assert ranked[0].company_match is True

    def test_no_target_company_no_match(self):
        cands = [_cand(primary_org="Foo AG")]
        ranked = rank_with_company(cands, "")
        assert ranked[0].company_match is False

    def test_null_primary_org_no_match(self):
        cands = [_cand(primary_org=None)]
        ranked = rank_with_company(cands, "Foo AG")
        assert ranked[0].company_match is False


# Integration tests — only runs when KG_PG_DSN env is set + docker-compose up.
# Setup:
#   docker compose up -d kg-postgres
#   export KG_PG_DSN=postgres://kg:kg_dev_only@localhost:55432/knowledge_graph
@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_kg_lookup_known_person():
    import os

    import asyncpg

    dsn = os.getenv("KG_PG_DSN")
    if not dsn:
        pytest.skip("KG_PG_DSN not set — skipping live KG integration test")

    from crm_check.graph.nodes.kg_lookup import lookup_kg

    conn = await asyncpg.connect(dsn)
    try:
        results = await lookup_kg(conn, "Frau Anna Beispiel", company="Beispiel")
        assert len(results) >= 1
        assert results[0].full_name == "Anna Beispiel"
        assert results[0].similarity_score > 0.9
        assert results[0].company_match is True
        assert results[0].is_active is True
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_kg_lookup_unknown_person():
    import os

    import asyncpg

    dsn = os.getenv("KG_PG_DSN")
    if not dsn:
        pytest.skip("KG_PG_DSN not set — skipping live KG integration test")

    from crm_check.graph.nodes.kg_lookup import lookup_kg

    conn = await asyncpg.connect(dsn)
    try:
        results = await lookup_kg(conn, "Herr Quirin Niemand-Bekannt")
        # Erwartet: kein Trigram-Hit über default-Schwelle (pg_trgm default 0.3)
        assert results == []
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_kg_lookup_inactive_with_stale_flags():
    """Friedrich Stale ist inactive + alle 3 Staleness-Flags True."""
    import os

    import asyncpg

    dsn = os.getenv("KG_PG_DSN")
    if not dsn:
        pytest.skip("KG_PG_DSN not set — skipping live KG integration test")

    from crm_check.graph.nodes.kg_lookup import lookup_kg

    conn = await asyncpg.connect(dsn)
    try:
        results = await lookup_kg(conn, "Herr Friedrich Stale")
        assert len(results) >= 1
        hit = results[0]
        assert hit.is_active is False
        assert hit.is_stale_linkedin is True
        assert hit.is_stale_wikidata is True
        assert hit.is_stale_ceq is True
    finally:
        await conn.close()
