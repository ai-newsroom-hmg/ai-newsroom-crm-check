"""KG-Lookup-Node — Trigram-Match gegen kg.person_universe.

SQL-Logic spiegelt exakt das Production-Schema aus
`hmg-knowledge-graph/init-db/10-person-universe.sql`:

- `normalized_full_name` ist GIN-Trigram-indexed (idx_pu_normalized_trgm)
- Operator `%` nutzt den Trigram-Index (pg_trgm) — single percent, nicht doppelt
- `similarity()` gibt den Match-Score 0..1 zurück

Company-Tiebreaker als optionaler 2. Schritt: wenn mehrere Trigram-Treffer
sehr nah beieinander liegen, gewichten wir die `primary_org`-ILIKE auf.
"""

from __future__ import annotations

from typing import Any

import asyncpg
from pydantic import BaseModel

from crm_check.normalize import name_for_matching


class KgCandidate(BaseModel):
    person_id: int
    wikidata_id: str | None
    full_name: str
    normalized_full_name: str
    role: str | None
    primary_org: str | None
    company_id: str | None
    linkedin_url: str | None
    last_seen: Any | None
    is_active: bool
    is_stale_linkedin: bool
    is_stale_wikidata: bool
    is_stale_ceq: bool
    similarity_score: float
    company_match: bool = False


_QUERY = """
SELECT
    person_id,
    wikidata_id,
    full_name,
    normalized_full_name,
    role,
    primary_org,
    company_id,
    linkedin_url,
    last_seen,
    is_active,
    is_stale_linkedin,
    is_stale_wikidata,
    is_stale_ceq,
    similarity(normalized_full_name, $1) AS similarity_score
FROM kg.person_universe
WHERE normalized_full_name % $1
ORDER BY similarity(normalized_full_name, $1) DESC
LIMIT $2
"""


def build_query(limit: int = 5) -> tuple[str, int]:
    """Exposed for tests — returns the SQL and limit so we can assert the
    operator + index hint never accidentally regress."""
    return _QUERY, limit


def rank_with_company(
    candidates: list[KgCandidate], target_company: str
) -> list[KgCandidate]:
    """Tiebreaker: bei ähnlichen Trigram-Scores hebt ein Firma-Match an die Spitze.

    Wir setzen `company_match=True` wenn `primary_org` substring von
    target_company oder umgekehrt ist (ILIKE-Semantik in Python). Final-Order:
    company_match desc, similarity desc.
    """
    target_norm = (target_company or "").casefold().strip()

    def has_match(c: KgCandidate) -> bool:
        if not target_norm or not c.primary_org:
            return False
        po = c.primary_org.casefold().strip()
        # robust gegen "GmbH" / "AG" / "& Co. KG"-Suffix-Variationen:
        # akzeptiere, wenn das kürzere Wort komplett im längeren steckt
        short, long = (po, target_norm) if len(po) <= len(target_norm) else (target_norm, po)
        return short in long

    annotated = [c.model_copy(update={"company_match": has_match(c)}) for c in candidates]
    annotated.sort(key=lambda c: (c.company_match, c.similarity_score), reverse=True)
    return annotated


async def lookup_kg(
    conn: asyncpg.Connection,
    salutation_name: str,
    company: str | None = None,
    limit: int = 5,
) -> list[KgCandidate]:
    """Führt den Trigram-Match aus und ranked optional mit Firma-Tiebreaker."""
    query_term = name_for_matching(salutation_name)
    if not query_term:
        return []

    sql, _ = build_query(limit)
    rows = await conn.fetch(sql, query_term, limit)
    raw = [KgCandidate(**dict(r)) for r in rows]
    if company:
        return rank_with_company(raw, company)
    return raw


async def open_pool(dsn: str) -> asyncpg.Pool:
    """asyncpg-Pool mit moderaten Defaults — Pool sollte vom Caller geschlossen
    werden (close() ist async).
    """
    return await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
