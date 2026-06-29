"""LangGraph-Wrapper um die existierenden Lookup-Funktionen.

Diese Nodes nehmen den CrmCheckState, ziehen die nötigen Felder, rufen den
async-Lookup auf und schreiben das Ergebnis in den entsprechenden State-Slot.

Connection-Pools + CEQ-Client kommen via Closure rein — wir injizieren sie
beim Graph-Build (build.py), damit die Nodes selbst stateless sind.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from crm_check.graph.claims_mapping import (
    ceq_to_claims,
    kg_entity_to_claims,
    kg_lobby_to_claims,
    kg_to_claims,
    ni_to_claims,
    openregister_company_to_claims,
    openregister_person_to_claims,
    wikidata_to_claims,
)
from crm_check.graph.nodes.kg_lobby_lookup import lookup_kg_entity, lookup_kg_lobby
from crm_check.graph.nodes.kg_lookup import lookup_kg
from crm_check.graph.nodes.ni_lookup import lookup_ni, rank_with_company
from crm_check.graph.nodes.openregister_node import lookup_openregister
from crm_check.graph.state import Claim, CrmCheckState

log = logging.getLogger(__name__)


NodeFn = Callable[[CrmCheckState], Awaitable[CrmCheckState]]


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _plausible(state: CrmCheckState, cand_first: str | None, cand_last: str | None) -> bool:
    """Plausibilitaets-Gate fuers Claim-Mapping.

    Pipeline-v2 2-Stufen-Suche: Stufe A (Identifikation) darf Position/Firma NICHT
    als Anker verwenden — sie sind die zu pruefende Behauptung. Hier nur Name.
    """
    first = (state.get("first_name") or "").casefold().strip()
    last = (state.get("last_name") or "").casefold().strip()
    if not last:
        return True   # ohne Last-Name kein Filter (paranoid waere False, aber zu eng)
    if not cand_last or cand_last.casefold().strip() != last:
        return False
    if first and cand_first:
        return cand_first.casefold().startswith(first[:3])
    return True


def make_kg_node(pool: asyncpg.Pool | None) -> NodeFn:
    """Probiert kg.person_universe (Cloud-GKE-Schema). Graceful-Skip wenn fehlt."""
    schema_present: dict[str, bool] = {}  # cache pro pool

    async def node(state: CrmCheckState) -> CrmCheckState:
        if not pool:
            return CrmCheckState(kg_candidates=[])
        t0 = time.monotonic()
        if schema_present.get("checked") and not schema_present.get("ok"):
            return CrmCheckState(kg_candidates=[], timings_ms={"kg": 0})
        try:
            async with pool.acquire() as conn:
                if "checked" not in schema_present:
                    exists = await conn.fetchval(
                        "SELECT to_regclass('kg.person_universe') IS NOT NULL"
                    )
                    schema_present["checked"] = True
                    schema_present["ok"] = bool(exists)
                    if not exists:
                        log.info("kg.person_universe not present — skipping kg_node")
                        return CrmCheckState(kg_candidates=[], timings_ms={"kg": _ms(t0)})
                cands = await lookup_kg(
                    conn,
                    state.get("salutation_name") or state.get("clean_name", ""),
                    company=state.get("company"),
                    limit=3,
                )
            # Claims-Mapping: nur plausible Kandidaten (Last-Name + First-Prefix)
            claims: list[Claim] = []
            for c in cands:
                # KG hat full_name aufgesplittet nicht direkt — parse last token
                parts = (c.full_name or "").split()
                first_, last_ = (parts[0] if parts else None), (parts[-1] if parts else None)
                if _plausible(state, first_, last_) and c.similarity_score >= 0.7:
                    claims.extend(kg_to_claims(c))
            return CrmCheckState(
                kg_candidates=cands,
                claims=claims,
                timings_ms={"kg": _ms(t0)},
            )
        except Exception as e:
            log.warning(f"kg_node: {e}")
            return CrmCheckState(
                kg_candidates=[],
                errors=[f"kg: {e}"],
                timings_ms={"kg": _ms(t0)},
            )
    return node


def make_kg_lobby_node(pool: asyncpg.Pool | None) -> NodeFn:
    async def node(state: CrmCheckState) -> CrmCheckState:
        if not pool:
            return CrmCheckState(kg_lobby_candidates=[], kg_entity_candidates=[])
        t0 = time.monotonic()
        try:
            async with pool.acquire() as conn:
                lobby = await lookup_kg_lobby(
                    conn,
                    state.get("last_name", ""),
                    first_name=state.get("first_name"),
                    company=state.get("company"),
                    limit=3,
                )
                # Plausibilität: First-Name oder Company match
                last = state.get("last_name", "").casefold()
                first = state.get("first_name", "").casefold()
                lobby = [
                    lb for lb in lobby
                    if lb.last_name.casefold() == last
                    and (not first or not lb.first_name
                         or lb.first_name.casefold().startswith(first[:3])
                         or lb.company_match)
                ]
                entities = await lookup_kg_entity(
                    conn,
                    state.get("clean_name", ""),
                    limit=3,
                )
                entities = [
                    e for e in entities if e.similarity_score >= 0.5
                ]
            # Claims-Mapping aus beiden Quellen
            claims: list[Claim] = []
            for lb in lobby:
                if _plausible(state, lb.first_name, lb.last_name):
                    claims.extend(kg_lobby_to_claims(lb))
            for e in entities:
                # kg.entities hat keine first/last-Splittung, voller Name reicht
                claims.extend(kg_entity_to_claims(e))
            return CrmCheckState(
                kg_lobby_candidates=lobby,
                kg_entity_candidates=entities,
                claims=claims,
                timings_ms={"kg_lobby": _ms(t0)},
            )
        except Exception as e:
            log.warning(f"kg_lobby_node: {e}")
            return CrmCheckState(
                kg_lobby_candidates=[],
                kg_entity_candidates=[],
                errors=[f"kg_lobby: {e}"],
            )
    return node


def make_ni_node(pool: asyncpg.Pool | None) -> NodeFn:
    """NI-Lookup mit Company-Anchored-Fallback + Position-Token-Boost.

    1. Person-direkt-Suche (ILIKE %name% OR %lastname%)
    2. Falls 0 Treffer: Company-anchored — alle NI-ORG-Entities zu Firma →
       gemeinsame Article-IDs → Personen die im selben Artikel erscheinen
       (Co-Mention). Hebel für Mittelstand-CEOs mit seltenen Mentions.
    """
    async def node(state: CrmCheckState) -> CrmCheckState:
        if not pool:
            return CrmCheckState(ni_candidates=[])
        t0 = time.monotonic()
        first = state.get("first_name", "").casefold()
        last = state.get("last_name", "").casefold()
        company = state.get("company") or ""
        position = (state.get("position") or "").casefold()
        try:
            async with pool.acquire() as conn:
                cands = await lookup_ni(
                    conn,
                    state.get("clean_name", ""),
                    last_name=state.get("last_name"),
                    company=company,
                    limit=8,
                )

                filtered = []
                for c in cands:
                    nm = (c.name or "").casefold()
                    plausible_name = (first and last and first in nm and last in nm)
                    if c.company_match or plausible_name:
                        filtered.append(c)

                # ENTFERNT (Pipeline-v2-Refactor): Position-Token-Boost auf company_match
                # war Confirmation-Bias — Position aus CRM darf nicht in die Person-
                # Identifikation einfliessen. Position-Mismatch ist jetzt
                # Contradiction-Penalty im correlate_node.
                # NOTE state.position weiter verwendet werden kann fuer Boosting in
                # spaeteren Phasen, aber nicht hier in der Identifikations-Stufe.
                _ = position  # absichtlich ungenutzt; wird in correlate_node geprueft

                cands = rank_with_company(filtered, company)
            # Claims-Mapping: nur plausible NI-Treffer
            claims: list[Claim] = []
            for c in cands:
                parts = (c.name or "").split()
                f_, l_ = (parts[0] if parts else None), (parts[-1] if parts else None)
                if _plausible(state, f_, l_):
                    claims.extend(ni_to_claims(c))
            return CrmCheckState(
                ni_candidates=cands,
                claims=claims,
                timings_ms={"ni": _ms(t0)},
            )
        except Exception as e:
            log.warning(f"ni_node: {e}")
            return CrmCheckState(ni_candidates=[], errors=[f"ni: {e}"])
    return node


def make_ceq_node(client: Any | None) -> NodeFn:
    async def node(state: CrmCheckState) -> CrmCheckState:
        if not client:
            return CrmCheckState(ceq_candidates=[])
        t0 = time.monotonic()
        try:
            name = state.get("clean_name", "")
            if not name:
                return CrmCheckState(ceq_candidates=[])
            hits = await client.search_persons(name)
            # Plausibilität: full_name muss first+last enthalten
            first = state.get("first_name", "").casefold()
            last = state.get("last_name", "").casefold()
            filtered = []
            for h in hits[:5]:
                fn = (h.full_name or "").casefold()
                if first and last and first in fn and last in fn:
                    filtered.append(h)
            # Claims-Mapping
            claims: list[Claim] = []
            for h in filtered:
                claims.extend(ceq_to_claims(h))
            return CrmCheckState(
                ceq_candidates=filtered,
                claims=claims,
                timings_ms={"ceq": _ms(t0)},
            )
        except Exception as e:
            log.warning(f"ceq_node: {e}")
            return CrmCheckState(ceq_candidates=[], errors=[f"ceq: {e}"])
    return node


def make_openregister_node() -> NodeFn:
    async def node(state: CrmCheckState) -> CrmCheckState:
        t0 = time.monotonic()
        try:
            res = await lookup_openregister(
                company=state.get("company"),
                person_name=state.get("clean_name"),
                crm_address=state.get("zip_city"),
                city=(state.get("zip_city") or "").split(" ", 1)[-1] if state.get("zip_city") else None,
            )
            persons = res.get("persons", [])
            companies = res.get("companies", [])
            # Claims-Mapping mit Plausibilitaets-Check fuer Personen
            claims: list[Claim] = []
            for p in persons:
                if _plausible(state, p.first_name, p.last_name):
                    claims.extend(openregister_person_to_claims(p))
            for c in companies:
                if c.person_match:
                    # person_match.name kommt aus dem Officer-Match — bereits gefiltert
                    claims.extend(openregister_company_to_claims(c))
            return CrmCheckState(
                openregister_candidates=companies,
                openregister_persons=persons,
                claims=claims,
                timings_ms={"openregister": _ms(t0)},
            )
        except Exception as e:
            log.warning(f"openregister_node: {e}")
            return CrmCheckState(
                openregister_candidates=[],
                openregister_persons=[],
                errors=[f"openregister: {e}"],
            )
    return node


def make_wikidata_node() -> NodeFn:
    from crm_check.graph.nodes.wikidata_node import lookup_wikidata_person

    async def node(state: CrmCheckState) -> CrmCheckState:
        t0 = time.monotonic()
        try:
            hits = await lookup_wikidata_person(
                full_name=state.get("clean_name", ""),
                company_hint=state.get("company"),
            )
            # Claims-Mapping mit Plausibilitaets-Check (Wikidata label muss zur Person passen)
            claims: list[Claim] = []
            for wd in hits:
                parts = (wd.label or "").split()
                f_, l_ = (parts[0] if parts else None), (parts[-1] if parts else None)
                if _plausible(state, f_, l_):
                    claims.extend(wikidata_to_claims(wd))
            return CrmCheckState(
                wikidata_hits=hits,
                claims=claims,
                timings_ms={"wikidata": _ms(t0)},
            )
        except Exception as e:
            log.warning(f"wikidata_node: {e}")
            return CrmCheckState(wikidata_hits=[], errors=[f"wikidata: {e}"])
    return node


def make_websearch_node(enabled: bool = True) -> NodeFn:
    """Conditional: nur wenn keine andere Quelle plausibel matched."""
    from crm_check.graph.nodes.websearch_node import websearch_person

    async def node(state: CrmCheckState) -> CrmCheckState:
        if not enabled:
            return CrmCheckState(websearch_results=[])
        # Skip if any structured source already produced plausible hit
        has_hit = bool(
            state.get("ni_candidates")
            or state.get("kg_lobby_candidates")
            or state.get("ceq_candidates")
            or state.get("openregister_persons")
            or state.get("wikidata_hits")
        )
        if has_hit:
            return CrmCheckState(websearch_results=[])
        t0 = time.monotonic()
        try:
            results = await websearch_person(
                first_name=state.get("first_name", ""),
                last_name=state.get("last_name", ""),
                company=state.get("company"),
                limit_per_query=4,
            )
            return CrmCheckState(
                websearch_results=results,
                timings_ms={"websearch": _ms(t0)},
            )
        except Exception as e:
            log.warning(f"websearch_node: {e}")
            return CrmCheckState(websearch_results=[], errors=[f"websearch: {e}"])
    return node
