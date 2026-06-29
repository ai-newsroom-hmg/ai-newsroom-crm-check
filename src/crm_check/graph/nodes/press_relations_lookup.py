"""PressRelations-Lookup gegen wraite Cloud-SQL — Pipeline-v2 Tier-2.

Quelle: `hypesignals_prod.press_relations_articles` (Parent-Table, 59,7 Mio. Artikel).
Topologie: lokal via cloud-sql-proxy auf 127.0.0.1:5434, GKE via wraite-proxy
in early-signals-Namespace. Schema-Spalten genutzt: date, domain, url, headline,
content, content_tsv (vorindexiert), sentiment, publication_reach.

INVARIANTE — STRIKT READ-ONLY:
- User `gunterclaude` ist technisch in `n8n_rw` (Schreibrechte vorhanden), darf
  ABER niemals schreibend genutzt werden. Dieser Node sendet ausschliesslich
  SELECT-Statements. Keine DDL, kein INSERT/UPDATE/DELETE/COPY/TRUNCATE.
- `application_name='crm-check-ro'` damit Read-Only-Last in pg_stat_activity
  sofort erkennbar ist.
- Connection-Pool min=1/max=2 — CRM-Check ist Lesegast, keine 10er-Last.

FTS-Pattern: `content_tsv @@ phraseto_tsquery('simple', 'Hans Mueller')`. 'simple'
weil deutsche Lemmatisierung bei Eigennamen mehr Schaden als Nutzen anrichtet.
Filtering auf Company-Tokens NACH FTS (ILIKE) damit der GIN-Index trifft.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import asyncpg
from pydantic import BaseModel


class PressRelationsHit(BaseModel):
    """Ein Treffer aus hypesignals_prod.press_relations_articles."""

    article_date: date | None = None
    domain: str | None = None
    url: str | None = None
    headline: str | None = None
    sentiment: float | None = None
    publication_reach: int | None = None
    company_match: bool = False
    snippet: str | None = None  # 200 Zeichen rund um Name-Match


_SQL = """
SELECT
    date::date                       AS article_date,
    domain,
    url,
    headline,
    sentiment,
    COALESCE(publication_reach, 0)   AS publication_reach,
    substring(content, 1, 240)       AS snippet
FROM hypesignals_prod.press_relations_articles
WHERE content_tsv @@ phraseto_tsquery('simple', $1)
  AND date >= (CURRENT_DATE - $2::int * INTERVAL '1 day')::date
ORDER BY date DESC, publication_reach DESC NULLS LAST
LIMIT $3
"""


def _matches_company(text: str | None, company: str) -> bool:
    if not text or not company:
        return False
    c = company.casefold().strip()
    # Suffix-Tokens entfernen damit "ACME GmbH" gegen "ACME" matched
    for suf in (" gmbh", " ag", " se", " kgaa", " mbh", " kg", " e.v.", " ev"):
        if c.endswith(suf):
            c = c[: -len(suf)].strip()
    return bool(c) and c in text.casefold()


async def lookup_press_relations(
    conn: asyncpg.Connection,
    full_name: str,
    *,
    company: str | None = None,
    days_back: int = 365,
    limit: int = 5,
) -> list[PressRelationsHit]:
    """Sucht Personen-Mentions im PressRelations-Korpus (Volltext)."""
    name = (full_name or "").strip()
    if not name:
        return []
    rows = await conn.fetch(_SQL, name, days_back, limit * 2)
    hits: list[PressRelationsHit] = []
    company_clean = (company or "").strip() or None
    for r in rows:
        d = dict(r)
        # Datum: asyncpg liefert date, vereinheitlich auf date
        ad = d.get("article_date")
        if isinstance(ad, datetime):
            ad = ad.date()
        hit = PressRelationsHit(
            article_date=ad,
            domain=d.get("domain"),
            url=d.get("url"),
            headline=d.get("headline"),
            sentiment=float(d["sentiment"]) if d.get("sentiment") is not None else None,
            publication_reach=int(d.get("publication_reach") or 0),
            snippet=d.get("snippet"),
        )
        if company_clean:
            hit.company_match = _matches_company(hit.headline, company_clean) or \
                                _matches_company(hit.snippet, company_clean)
        hits.append(hit)

    # Company-Match-Treffer zuerst; dann nach Reach/Datum
    hits.sort(
        key=lambda h: (
            h.company_match,
            h.publication_reach or 0,
            h.article_date or date(1900, 1, 1),
        ),
        reverse=True,
    )
    return hits[:limit]


def build_query() -> tuple[str, Any]:
    """Test-Hook fuer SQL-Inspection (keine Connection benoetigt)."""
    return _SQL, None
