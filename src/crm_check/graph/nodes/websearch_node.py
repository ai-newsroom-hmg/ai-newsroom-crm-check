"""WebSearch-Node — SearXNG-Aggregator-Suche.

Ersetzt Perplexity durch self-hosted SearXNG (`SEARXNG_URL`-Env, Default
`http://127.0.0.1:8888`). SearXNG aggregiert Google/Bing/DDG/Qwant kostenlos
in eine JSON-API.

Strategie pro CRM-Zeile:
  Query A: "{first} {last} {company}"     — Position/Firma-Validierung
  Query B: "{first} {last} site:linkedin.com"  — Profile-URL
  Query C: "{first} {last} {company} GF Vorstand"  — Rolle direkt

Conditional: nur ausgeführt wenn KG/NI/CEQ/OR keinen plausiblen Person-Match
liefern (lookup_count == 0) ODER wenn `force_websearch=True`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

DEFAULT_SEARXNG_URL = "http://127.0.0.1:8888"


class WebSearchHit(BaseModel):
    title: str
    url: str
    snippet: str | None = None
    engine: str | None = None
    score: float | None = None


class WebSearchResult(BaseModel):
    query: str
    hits: list[WebSearchHit] = Field(default_factory=list)


def _searxng_url() -> str:
    return os.environ.get("SEARXNG_URL", DEFAULT_SEARXNG_URL).rstrip("/")


async def _query(http, q: str, limit: int = 5) -> WebSearchResult:
    try:
        resp = await http.get(
            f"{_searxng_url()}/search",
            params={"q": q, "format": "json", "language": "de"},
        )
        resp.raise_for_status()
        data = resp.json()
        hits = []
        for r in (data.get("results") or [])[:limit]:
            hits.append(WebSearchHit(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content"),
                engine=r.get("engine"),
                score=r.get("score"),
            ))
        return WebSearchResult(query=q, hits=hits)
    except Exception as e:
        log.warning(f"searxng '{q}': {e}")
        return WebSearchResult(query=q, hits=[])


async def websearch_person(
    *,
    first_name: str,
    last_name: str,
    company: str | None = None,
    limit_per_query: int = 5,
) -> list[WebSearchResult]:
    """3 parallele Queries: Standard + LinkedIn + Rolle."""
    if not (first_name and last_name):
        return []
    try:
        import httpx
    except ImportError:
        return []

    person = f"{first_name} {last_name}".strip()
    queries = [f'"{person}" {company or ""}'.strip()]
    # LinkedIn-Profile-Queries: zwei Varianten, damit Brave/DDG/Qwant mehr
    # Profile-Hits liefern (Recall-Hebel statt nur ein generischer site:-Query):
    if company:
        # Company-spezifischer LinkedIn-Query — exaktester Hit
        queries.append(f'"{person}" "{company}" site:linkedin.com/in')
    queries.append(f'"{person}" site:linkedin.com/in')
    if company:
        queries.append(f'"{person}" "{company}" Vorstand OR Geschäftsführer OR CEO')

    async with httpx.AsyncClient(timeout=15.0) as http:
        results = await asyncio.gather(
            *(_query(http, q, limit_per_query) for q in queries),
            return_exceptions=True,
        )
    return [r for r in results if isinstance(r, WebSearchResult)]
