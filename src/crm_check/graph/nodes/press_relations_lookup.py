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
WITH person_q AS (
    SELECT phraseto_tsquery('simple', $1) AS q
),
company_q AS (
    SELECT CASE
             WHEN COALESCE($4, '') = '' THEN NULL
             ELSE plainto_tsquery('simple', $4)
           END AS q
),
hits AS (
    SELECT
        a.date::date                                              AS article_date,
        a.domain,
        a.url,
        a.headline,
        a.sentiment,
        COALESCE(a.publication_reach, 0)                          AS publication_reach,
        ts_headline(
            'simple', a.content, pq.q,
            'StartSel=<<,StopSel=>>,MaxWords=35,MinWords=15,ShortWord=2,MaxFragments=2,FragmentDelimiter= ... '
        )                                                          AS snippet,
        ts_rank_cd(a.content_tsv, pq.q, 32)                       AS rank_person,
        CASE
            WHEN cq.q IS NULL THEN 0.0
            WHEN a.content_tsv @@ cq.q THEN ts_rank_cd(a.content_tsv, cq.q, 32)
            ELSE 0.0
        END                                                        AS rank_company,
        (cq.q IS NOT NULL AND a.content_tsv @@ cq.q)              AS company_match_fts
    FROM hypesignals_prod.press_relations_articles a
    CROSS JOIN person_q pq
    LEFT  JOIN company_q cq ON TRUE
    WHERE a.content_tsv @@ pq.q
      AND a.date >= (CURRENT_DATE - $2::int * INTERVAL '1 day')::date
)
SELECT
    article_date, domain, url, headline, sentiment, publication_reach, snippet,
    company_match_fts
FROM hits
ORDER BY
    company_match_fts DESC,
    (rank_person + rank_company * 1.5) DESC,
    article_date DESC,
    publication_reach DESC NULLS LAST
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
    company_clean = (company or "").strip()
    # Strip Suffix-Tokens fuer die NLP-Query damit "ACME GmbH" gegen "ACME" matched
    company_token = company_clean
    for suf in (" gmbh", " ag", " se", " kgaa", " mbh", " kg", " e.v.", " ev"):
        if company_token.lower().endswith(suf):
            company_token = company_token[: -len(suf)].strip()
    rows = await conn.fetch(_SQL, name, days_back, limit, company_token or "")
    hits: list[PressRelationsHit] = []
    for r in rows:
        d = dict(r)
        ad = d.get("article_date")
        if isinstance(ad, datetime):
            ad = ad.date()
        # FTS-basiertes company_match aus dem SQL — semantisch staerker als ILIKE,
        # da es Lemmatisierung + Wortgrenzen kennt.
        hit = PressRelationsHit(
            article_date=ad,
            domain=d.get("domain"),
            url=d.get("url"),
            headline=d.get("headline"),
            sentiment=float(d["sentiment"]) if d.get("sentiment") is not None else None,
            publication_reach=int(d.get("publication_reach") or 0),
            snippet=d.get("snippet"),
            company_match=bool(d.get("company_match_fts")) if company_token else False,
        )
        hits.append(hit)
    # SQL ist bereits sortiert (company_match desc, rank desc, date desc) — keine Python-Resort.
    return hits


def build_query() -> tuple[str, Any]:
    """Test-Hook fuer SQL-Inspection (keine Connection benoetigt)."""
    return _SQL, None
