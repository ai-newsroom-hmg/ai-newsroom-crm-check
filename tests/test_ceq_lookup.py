"""CEQ-Lookup-Tests — Unit (Ranking) + Integration (live API)."""

import os

import pytest

from crm_check.graph.nodes.ceq_lookup import CeqPerson, rank_persons_by_company


def _person(**kw) -> CeqPerson:
    base = dict(full_name="Test", company_name=None)
    base.update(kw)
    return CeqPerson(**base)


class TestRankPersonsByCompany:
    def test_match_promotes(self):
        ps = [
            _person(full_name="A", company_name="OtherCorp"),
            _person(full_name="B", company_name="Trend Micro Deutschland GmbH"),
        ]
        ranked = rank_persons_by_company(ps, "Trend Micro")
        assert ranked[0][0].full_name == "B"
        assert ranked[0][1] is True
        assert ranked[1][1] is False

    def test_short_in_long(self):
        ps = [_person(company_name="McKinsey")]
        ranked = rank_persons_by_company(ps, "McKinsey & Company, Inc.")
        assert ranked[0][1] is True

    def test_no_target(self):
        ps = [_person(company_name="Anything")]
        ranked = rank_persons_by_company(ps, "")
        assert ranked[0][1] is False

    def test_null_company(self):
        ps = [_person(company_name=None)]
        ranked = rank_persons_by_company(ps, "Foo")
        assert ranked[0][1] is False


# Live tests against production CEQ-API.
# Run: export CEQ_API_URL=http://100.78.225.57:8443 CEQ_API_TOKEN=...
@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_ceq_health():
    url = os.getenv("CEQ_API_URL")
    token = os.getenv("CEQ_API_TOKEN")
    if not (url and token):
        pytest.skip("CEQ_API_URL/CEQ_API_TOKEN not set")
    from crm_check.graph.nodes.ceq_lookup import CeqClient

    async with CeqClient(url, token) as client:
        h = await client.health()
        assert h["ok"] is True
        assert "rows" in h
        assert h["rows"] > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_ceq_search_political_figure():
    """Politische Personen sind im CEQ — daher Robust-Test mit bekanntem Namen."""
    url = os.getenv("CEQ_API_URL")
    token = os.getenv("CEQ_API_TOKEN")
    if not (url and token):
        pytest.skip("CEQ_API_URL/CEQ_API_TOKEN not set")
    from crm_check.graph.nodes.ceq_lookup import CeqClient

    async with CeqClient(url, token) as client:
        hits = await client.search_persons("Merz")
        # Friedrich Merz / weitere Merz-Treffer erwartet
        assert len(hits) >= 1
        assert any("Merz" in p.full_name for p in hits)
