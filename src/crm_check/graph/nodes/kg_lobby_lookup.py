"""KG-Lobby-Lookup — Lobbyregister-Personen aus kg.lobby_persons + kg.entities.

`kg.lobby_persons` enthält offizielle Bundestags-Lobbyregister-Einträge mit
function/role/Organisation (über `org_entity_id` → kg.entities). Trigram-Index
auf `last_name` (`idx_kg_lpers_name`) macht Suche schnell.

Zusätzlich Trigram auf `kg.entities.canonical_name` (`idx_kg_entities_name`) für
generelle PERSON-Entities (auch wenn nicht im Lobbyregister).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg
from pydantic import BaseModel


class KgLobbyCandidate(BaseModel):
    lobby_id: int
    register_number: str
    first_name: str | None = None
    last_name: str
    academic_degree: str | None = None
    function: str | None = None
    role: str
    gov_function_present: bool = False
    gov_position: str | None = None
    gov_authority: str | None = None
    gov_end_date: str | None = None
    org_name: str | None = None
    synced_at: datetime | None = None
    similarity_score: float = 0.0
    company_match: bool = False


class KgEntityCandidate(BaseModel):
    entity_id: int
    canonical_name: str
    entity_type: str
    wikidata_id: str | None = None
    total_mentions: int = 0
    last_mentioned: datetime | None = None
    aliases: list[str] = []
    similarity_score: float = 0.0


_SQL_LOBBY = """
SELECT lp.id                       AS lobby_id,
       lp.register_number,
       lp.first_name,
       lp.last_name,
       lp.academic_degree,
       lp.function,
       lp.role,
       lp.gov_function_present,
       lp.gov_position,
       lp.gov_authority,
       lp.gov_end_date,
       org.canonical_name           AS org_name,
       lp.synced_at,
       (similarity(lp.last_name, $1)
        + COALESCE(similarity(lp.first_name, $2), 0)) / 2.0 AS similarity_score
  FROM kg.lobby_persons lp
  LEFT JOIN kg.entities org ON org.id = lp.org_entity_id
 WHERE lp.last_name % $1
 ORDER BY similarity_score DESC, lp.synced_at DESC NULLS LAST
 LIMIT $3
"""

# Company-anchored: finde alle Lobby-Personen einer Firma. Hebel für
# Mittelstand-CEOs wo kein Pressemention da ist aber die Firma Lobbying betreibt.
_SQL_LOBBY_BY_COMPANY = """
SELECT lp.id                       AS lobby_id,
       lp.register_number,
       lp.first_name,
       lp.last_name,
       lp.academic_degree,
       lp.function,
       lp.role,
       lp.gov_function_present,
       lp.gov_position,
       lp.gov_authority,
       lp.gov_end_date,
       org.canonical_name           AS org_name,
       lp.synced_at,
       (COALESCE(similarity(lp.last_name, $1), 0)
        + COALESCE(similarity(lp.first_name, $2), 0)
        + similarity(org.canonical_name, $3)) / 3.0 AS similarity_score
  FROM kg.lobby_persons lp
  JOIN kg.entities org ON org.id = lp.org_entity_id
 WHERE org.canonical_name % $3
 ORDER BY similarity_score DESC, lp.synced_at DESC NULLS LAST
 LIMIT $4
"""


_SQL_ENTITY = """
SELECT id                           AS entity_id,
       canonical_name,
       entity_type,
       wikidata_id,
       total_mentions,
       last_mentioned,
       aliases,
       similarity(canonical_name, $1) AS similarity_score
  FROM kg.entities
 WHERE entity_type = 'PERSON'
   AND canonical_name % $1
 ORDER BY similarity(canonical_name, $1) DESC, total_mentions DESC
 LIMIT $2
"""


def rank_lobby_with_company(
    candidates: list[KgLobbyCandidate], target_company: str
) -> list[KgLobbyCandidate]:
    target = (target_company or "").casefold().strip()

    def matched(c: KgLobbyCandidate) -> bool:
        if not target or not c.org_name:
            return False
        cn = c.org_name.casefold().strip()
        short, long_ = (cn, target) if len(cn) <= len(target) else (target, cn)
        return bool(short) and short in long_

    for c in candidates:
        c.company_match = matched(c)
    candidates.sort(
        key=lambda c: (c.company_match, c.similarity_score), reverse=True
    )
    return candidates


async def lookup_kg_lobby(
    conn: asyncpg.Connection,
    last_name: str,
    *,
    first_name: str | None = None,
    company: str | None = None,
    limit: int = 5,
) -> list[KgLobbyCandidate]:
    """Last-Name-Trigram-Suche + optional Company-Anchored-Cross-Check.

    Wenn Company gesetzt: zweite Query nach `org.canonical_name % company`,
    dann union+dedupe nach lobby_id. Hebel: Mittelstand-CEOs die NICHT direkt
    als Lobbyist auftauchen aber die Firma Lobbying betreibt.
    """
    if not last_name.strip():
        return []
    rows = await conn.fetch(_SQL_LOBBY, last_name.strip(), (first_name or "").strip(), limit)
    cands: list[KgLobbyCandidate] = [KgLobbyCandidate(**dict(r)) for r in rows]

    if company and company.strip():
        try:
            extra = await conn.fetch(
                _SQL_LOBBY_BY_COMPANY,
                last_name.strip(), (first_name or "").strip(),
                company.strip(), limit,
            )
            seen = {c.lobby_id for c in cands}
            for r in extra:
                rec = KgLobbyCandidate(**dict(r))
                if rec.lobby_id not in seen:
                    cands.append(rec)
                    seen.add(rec.lobby_id)
        except Exception:  # noqa: BLE001 — Fallback graceful
            pass

    if company:
        cands = rank_lobby_with_company(cands, company)
    return cands


async def lookup_kg_entity(
    conn: asyncpg.Connection,
    full_name: str,
    *,
    limit: int = 5,
) -> list[KgEntityCandidate]:
    if not full_name.strip():
        return []
    rows = await conn.fetch(_SQL_ENTITY, full_name.strip(), limit)
    return [KgEntityCandidate(**dict(r)) for r in rows]


def build_queries() -> tuple[str, str, Any]:
    return _SQL_LOBBY, _SQL_ENTITY, None
