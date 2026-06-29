"""CEQ-API-Lookup — Trigram-Search gegen rankings.person_profiles.

CEQ-API ist die Production-REST-Hülle vor `rankings.person_profiles`
(ditschserver, Phase 2.3 LIVE seit 2026-06-23, ~2856 Personen).

Endpoints (aus `~/Projects/ceq-api/src/ceq_api/routers/persons.py`):
- `GET /v1/persons/{qid}` — Lookup per Wikidata-ID
- `GET /v1/persons?qids=Q1,Q2,...` — Batch, max 500
- `GET /v1/persons?company_id=...`
- `GET /v1/persons?search=<name>` — Trigram, max 50

Auth: `Authorization: Bearer <token>` (Token-Whitelist server-side).

Known issue (2026-06-27): ceq-api listening auf `*:8443` ist PLAIN HTTP
(Tailscale-Serve TLS-Termination ist nicht aktiv). Daher CEQ_API_URL
mit `http://` prefix — wird in Phase 3 mit echtem TLS gefixt.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel

log = logging.getLogger(__name__)


class CeqPerson(BaseModel):
    """Subset der CEQ-API Response — die für CRM-Check relevanten Felder."""

    person_id: str | None = None
    wikidata_id: str | None = None
    full_name: str
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    role: str | None = None
    company_id: str | None = None
    company_name: str | None = None
    dax_index: str | None = None
    in_role_since: str | None = None  # date
    appointed_until: str | None = None  # date — wichtig für "Mandat beendet"
    linkedin_url: str | None = None
    linkedin_followers: int | None = None
    wikipedia_url: str | None = None
    twitter_url: str | None = None
    image_url: str | None = None
    salary_current: float | None = None
    scraping_active: bool | None = None
    source: str | None = None
    updated_date: str | None = None  # Aktualitäts-Stempel
    person_subtype: str | None = None
    b2p_b2c: str | None = None


class CeqClient:
    """Async-Client gegen ceq-api. Wird vom CLI + LangGraph-Node geteilt."""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout

    async def __aenter__(self) -> "CeqClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        r = await self._client.get("/v1/health")
        r.raise_for_status()
        return r.json()

    async def search_persons(self, name: str, *, limit_hint: int = 50) -> list[CeqPerson]:
        """Trigram-Search; CEQ-API gibt bis zu 50 Treffer zurück."""
        if not name.strip():
            return []
        r = await self._client.get("/v1/persons", params={"search": name})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        return [CeqPerson(**row) for row in data[:limit_hint]]


def rank_persons_by_company(
    candidates: list[CeqPerson], target_company: str
) -> list[tuple[CeqPerson, bool]]:
    """Gibt (person, company_match)-Paare zurück, sortiert: company_match first."""
    target = (target_company or "").casefold().strip()

    def matched(p: CeqPerson) -> bool:
        if not target:
            return False
        for cand in (p.company_name, p.company_id):
            if not cand:
                continue
            cn = cand.casefold().strip()
            short, long_ = (cn, target) if len(cn) <= len(target) else (target, cn)
            if short and short in long_:
                return True
        return False

    annotated = [(p, matched(p)) for p in candidates]
    annotated.sort(key=lambda t: (t[1],), reverse=True)
    return annotated
