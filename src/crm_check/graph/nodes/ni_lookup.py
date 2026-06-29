"""News-Intelligence-Lookup — sucht eine Person in ni.entities + ni.entity_profiles.

`ni.entities` indexiert alle in RSS-Feeds erwähnten Personen (PER-Entities). Der
Profile-Join liefert role + primary_org + segment — vom apposition-Extractor des
news-intelligence-Services im Vorfeld bestimmt.

WICHTIG: Genios ist im Vault TABU (Memory `feedback_genios_human_pagination...`).
NI ingestiert per RSS aktuell keine Genios-Quellen, aber wir schließen
`feed_name='Genios'` defensiv aus.

NI hat KEIN `pg_trgm` — daher ILIKE-Suche auf `name`. Performance: ~70ms pro
PER-Suche bei 143k PER-Entities (gemessen 2026-06-27).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg
from pydantic import BaseModel


class NiCandidate(BaseModel):
    """Subset des NI-Lookup-Resultats."""

    entity_id: int
    name: str
    wikidata_id: str | None = None
    mention_count: int
    primary_subtype: str | None = None
    segment: str | None = None
    role: str | None = None
    primary_org: str | None = None
    confidence: float | None = None
    profile_updated_at: datetime | None = None
    last_mention_at: datetime | None = None
    last_article_title: str | None = None
    last_article_domain: str | None = None
    last_article_url: str | None = None
    company_match: bool = False


# Beobachtung: NI-Entity-Namen enthalten oft "Vorname Nachname". Wir suchen mit
# beiden Anteilen damit "Frank Schwittay" gegen Entity "Frank Schwittay" matcht
# und auch reine Last-Name-Treffer wie "Schwittay" hochkommen.
_SQL = """
WITH base AS (
    SELECT e.id, e.name, e.wikidata_id, e.mention_count
    FROM ni.entities e
    WHERE e.type = 'PER'
      AND (e.name ILIKE $1 OR e.name ILIKE $2)
    ORDER BY e.mention_count DESC
    LIMIT $3
)
SELECT b.id           AS entity_id,
       b.name,
       b.wikidata_id,
       b.mention_count,
       p.primary_subtype,
       p.segment,
       p.role,
       p.primary_org,
       p.confidence,
       p.updated_at   AS profile_updated_at,
       (SELECT a.published_at
          FROM ni.entity_mentions m
          JOIN ni.articles a ON a.id = m.article_id
         WHERE m.entity_id = b.id
           AND COALESCE(a.feed_name, '') <> 'Genios'
         ORDER BY a.published_at DESC NULLS LAST
         LIMIT 1)        AS last_mention_at,
       (SELECT a.title
          FROM ni.entity_mentions m
          JOIN ni.articles a ON a.id = m.article_id
         WHERE m.entity_id = b.id
           AND COALESCE(a.feed_name, '') <> 'Genios'
         ORDER BY a.published_at DESC NULLS LAST
         LIMIT 1)        AS last_article_title,
       (SELECT a.domain
          FROM ni.entity_mentions m
          JOIN ni.articles a ON a.id = m.article_id
         WHERE m.entity_id = b.id
           AND COALESCE(a.feed_name, '') <> 'Genios'
         ORDER BY a.published_at DESC NULLS LAST
         LIMIT 1)        AS last_article_domain,
       (SELECT a.url
          FROM ni.entity_mentions m
          JOIN ni.articles a ON a.id = m.article_id
         WHERE m.entity_id = b.id
           AND COALESCE(a.feed_name, '') <> 'Genios'
         ORDER BY a.published_at DESC NULLS LAST
         LIMIT 1)        AS last_article_url
  FROM base b
  LEFT JOIN ni.entity_profiles p ON p.entity_id = b.id
"""


def rank_with_company(
    candidates: list[NiCandidate], target_company: str
) -> list[NiCandidate]:
    target = (target_company or "").casefold().strip()

    def matched(c: NiCandidate) -> bool:
        if not target or not c.primary_org:
            return False
        cn = c.primary_org.casefold().strip()
        short, long_ = (cn, target) if len(cn) <= len(target) else (target, cn)
        return bool(short) and short in long_

    annotated: list[NiCandidate] = []
    for c in candidates:
        c.company_match = matched(c)
        annotated.append(c)
    annotated.sort(key=lambda c: (c.company_match, c.mention_count), reverse=True)
    return annotated


async def lookup_ni(
    conn: asyncpg.Connection,
    full_name: str,
    *,
    last_name: str | None = None,
    company: str | None = None,
    limit: int = 5,
) -> list[NiCandidate]:
    """Sucht Person in NI-Entities (ILIKE Full-Name + Last-Name) + Profile + last_mention."""
    name = full_name.strip()
    if not name:
        return []
    full_pattern = f"%{name}%"
    last_pattern = f"%{(last_name or name.split()[-1]).strip()}%"
    rows = await conn.fetch(_SQL, full_pattern, last_pattern, limit)
    cands = [NiCandidate(**dict(r)) for r in rows]
    if company:
        cands = rank_with_company(cands, company)
    return cands


def build_query() -> tuple[str, Any]:
    """Test-Hook für SQL-Inspection."""
    return _SQL, None
