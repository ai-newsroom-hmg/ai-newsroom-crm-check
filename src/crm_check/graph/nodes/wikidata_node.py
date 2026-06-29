"""Wikidata-Node — SPARQL für Person-Identifikation + Position + Social-URLs.

Pattern aus `vault-tools/wikidata_social_enrich.py`: kein API-Key, kein
Rate-Limit-Issue solange wir <5 Req/Sek + User-Agent setzen.

SPARQL-Strategie (Wikidata-Properties):
  P31  = instance of (Q5 = Mensch)
  P39  = position held (mit qualifier P580 start / P582 end)
  P108 = employer
  P2002 = Twitter-Handle
  P6634 = LinkedIn-Personal-Profile-ID
  P569 = date of birth
  P19  = place of birth

Wir laufen 2 Queries:
  1. Direkt-Match auf Label "Vorname Nachname" (de-Label)
  2. Falls leer + Firma vorhanden: search by employer (P108)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "ai-newsroom-crm-check/0.1 (gunter.nowy@almagenic.com)"


class WikidataPersonHit(BaseModel):
    qid: str                            # Q-ID, z.B. Q42
    label: str
    description: str | None = None
    occupation: str | None = None       # P106-Label
    current_position: str | None = None  # P39 ohne end_time
    current_employer: str | None = None  # P108-Label
    employer_qid: str | None = None
    linkedin_id: str | None = None
    twitter_handle: str | None = None
    wikipedia_url: str | None = None
    date_of_birth: str | None = None


# SPARQL: Personen mit gegebenem Label, mit current-position+employer
_SPARQL_PERSON = """
SELECT DISTINCT ?p ?pLabel ?desc ?pos ?posLabel ?emp ?empLabel ?linkedin ?twitter ?dob ?wpUrl WHERE {
  ?p rdfs:label ?label .
  FILTER(LANG(?label) IN ("de", "en"))
  FILTER(LCASE(STR(?label)) = LCASE(?name))
  ?p wdt:P31 wd:Q5 .
  OPTIONAL { ?p schema:description ?desc . FILTER(LANG(?desc) = "de") }
  OPTIONAL {
    ?p p:P39 ?posStmt .
    ?posStmt ps:P39 ?pos .
    FILTER NOT EXISTS { ?posStmt pq:P582 [] }
  }
  OPTIONAL {
    ?p p:P108 ?empStmt .
    ?empStmt ps:P108 ?emp .
    FILTER NOT EXISTS { ?empStmt pq:P582 [] }
  }
  OPTIONAL { ?p wdt:P6634 ?linkedin }
  OPTIONAL { ?p wdt:P2002 ?twitter }
  OPTIONAL { ?p wdt:P569 ?dob }
  OPTIONAL {
    ?wpUrl schema:about ?p ;
           schema:isPartOf <https://de.wikipedia.org/> .
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "de,en" }
}
LIMIT 10
"""


import asyncio
import time

# Modul-globaler Rate-Limiter + Cache (process-life).
# Wikidata 2026-Outage erzwingt "1 req/min" — wir nutzen Soft-Disable nach 1
# 429-Antwort und Re-Try erst nach 60s, plus pro-Name-Cache.
_RATE_GATE = {"blocked_until": 0.0, "lock": asyncio.Lock()}
_CACHE: dict[str, list["WikidataPersonHit"]] = {}


async def lookup_wikidata_person(
    *,
    full_name: str,
    company_hint: str | None = None,
) -> list[WikidataPersonHit]:
    """SPARQL gegen wd:Q5 mit Label-Match. Mit Rate-Limit-Soft-Disable."""
    name = (full_name or "").strip()
    if not name:
        return []
    cache_key = name.casefold()
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    if time.monotonic() < _RATE_GATE["blocked_until"]:
        return []

    try:
        import httpx
    except ImportError:
        return []

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/sparql-results+json",
    }
    query = _SPARQL_PERSON.replace(
        "?name",
        f'"{name.replace(chr(34), chr(92) + chr(34))}"',
    )

    async with _RATE_GATE["lock"]:
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=headers) as c:
                resp = await c.get(
                    WIKIDATA_SPARQL,
                    params={"query": query, "format": "json"},
                )
                if resp.status_code == 429:
                    _RATE_GATE["blocked_until"] = time.monotonic() + 60.0
                    log.info("wikidata 429 — skipping further calls for 60s")
                    return []
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            log.warning(f"wikidata SPARQL '{name}': {e}")
            return []

    hits_by_qid: dict[str, WikidataPersonHit] = {}
    for b in data.get("results", {}).get("bindings", []):
        qid_url = b.get("p", {}).get("value", "")
        qid = qid_url.rsplit("/", 1)[-1] if qid_url else None
        if not qid:
            continue
        h = hits_by_qid.get(qid)
        if not h:
            h = WikidataPersonHit(
                qid=qid,
                label=b.get("pLabel", {}).get("value") or name,
                description=b.get("desc", {}).get("value"),
                current_position=b.get("posLabel", {}).get("value"),
                current_employer=b.get("empLabel", {}).get("value"),
                employer_qid=(b.get("emp", {}).get("value", "").rsplit("/", 1)[-1] or None) or None,
                linkedin_id=b.get("linkedin", {}).get("value"),
                twitter_handle=b.get("twitter", {}).get("value"),
                wikipedia_url=b.get("wpUrl", {}).get("value"),
                date_of_birth=b.get("dob", {}).get("value"),
            )
            hits_by_qid[qid] = h
        else:
            # Mehrere Positions/Employer-Rows: take first non-None
            h.current_position = h.current_position or b.get("posLabel", {}).get("value")
            h.current_employer = h.current_employer or b.get("empLabel", {}).get("value")

    hits = list(hits_by_qid.values())
    if company_hint:
        ch = company_hint.casefold().strip()
        hits.sort(
            key=lambda h: bool(h.current_employer and ch in h.current_employer.casefold()),
            reverse=True,
        )
    _CACHE[cache_key] = hits
    return hits


def linkedin_url(handle_or_id: str | None) -> str | None:
    """P6634 ist die LinkedIn-ID — URL bauen."""
    if not handle_or_id:
        return None
    if handle_or_id.startswith("http"):
        return handle_or_id
    return f"https://www.linkedin.com/in/{handle_or_id}/"


def twitter_url(handle: str | None) -> str | None:
    if not handle:
        return None
    if handle.startswith("http"):
        return handle
    return f"https://x.com/{handle.lstrip('@')}"
