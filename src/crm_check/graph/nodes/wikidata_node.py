"""Wikidata-Node — 2-Stufen-Lookup nach Vault-Doku B2 (Entity Intelligence).

Stufe 1: wbsearchentities-API für Fuzzy-Match (Label → QID-Kandidaten)
Stufe 2: wbgetentities-API für Property-Pull (claims + labels + sitelinks)

Warum NICHT WDQS-SPARQL? Wikidata-Query-Service ist 2026 in aktivem Outage
("Aggressively rate-limiting to 1 req / min" — wdqs outage 797a132). Die
Entity-API liefert die gleichen Properties JSON-strukturiert, ist nicht von
WDQS-Drosselung betroffen und schneller (kein SPARQL-Parse-Overhead).

Properties (Vault-Doku B2):
  P31   = instance of (Q5 = Mensch — Pre-Filter)
  P39   = position held (qualifier P582 end_time → aktuell-Filter)
  P106  = occupation (Beruf — Fallback wenn P39 leer)
  P108  = employer (qualifier P582 end_time → aktuell-Filter)
  P569  = date of birth
  P570  = date of death (Pre-Filter → Deceased flag)
  P2002 = Twitter-Handle
  P6634 = LinkedIn-Personal-Profile-ID
  P4003 = Facebook-ID
  P856  = official website
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "ai-newsroom-crm-check/0.1 (gunter.nowy@almagenic.com)"


class WikidataPersonHit(BaseModel):
    qid: str
    label: str
    description: str | None = None
    occupation: str | None = None       # P106-Label (fallback wenn P39 leer)
    current_position: str | None = None  # P39 ohne end_time
    current_employer: str | None = None  # P108-Label
    employer_qid: str | None = None
    linkedin_id: str | None = None
    twitter_handle: str | None = None
    facebook_id: str | None = None
    website_url: str | None = None
    wikipedia_url: str | None = None
    date_of_birth: str | None = None
    is_deceased: bool = False


# Process-life cache + soft rate-gate
_RATE_GATE: dict[str, Any] = {"blocked_until": 0.0, "lock": asyncio.Lock()}
_CACHE: dict[str, list["WikidataPersonHit"]] = {}
_QID_CACHE: dict[str, list[str]] = {}
_LABEL_CACHE: dict[str, str] = {}


async def _resolve_qids(
    name: str,
    *,
    max_results: int = 3,
    language: str = "de",
) -> list[str]:
    """Stufe 1: wbsearchentities → bis zu `max_results` QIDs für `name`.

    Fragt zuerst `de`, fällt auf `en` zurück wenn 0 Treffer.
    """
    if not name:
        return []
    cache_key = f"{language}::{name.casefold()}"
    if cache_key in _QID_CACHE:
        return _QID_CACHE[cache_key]

    try:
        import httpx
    except ImportError:
        return []

    headers = {"User-Agent": USER_AGENT}
    params = {
        "action": "wbsearchentities",
        "search": name,
        "language": language,
        "format": "json",
        "type": "item",
        "limit": str(max_results),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as c:
            resp = await c.get(WIKIDATA_API, params=params)
            if resp.status_code == 429:
                _RATE_GATE["blocked_until"] = time.monotonic() + 60.0
                return []
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning(f"wbsearchentities '{name}' [{language}]: {e}")
        return []

    qids = [item["id"] for item in data.get("search", []) if item.get("id", "").startswith("Q")]
    if not qids and language == "de":
        qids = await _resolve_qids(name, max_results=max_results, language="en")
    _QID_CACHE[cache_key] = qids
    return qids


async def _wb_get(params: dict[str, str], *, timeout: float = 15.0) -> dict | None:
    """Wrapper für wbgetentities mit Rate-Gate."""
    if time.monotonic() < _RATE_GATE["blocked_until"]:
        return None
    try:
        import httpx
    except ImportError:
        return None
    headers = {"User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as c:
            resp = await c.get(WIKIDATA_API, params=params)
            if resp.status_code == 429:
                _RATE_GATE["blocked_until"] = time.monotonic() + 60.0
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        log.warning(f"wbgetentities {params.get('ids', '?')}: {e}")
        return None


def _statement_value(claim: dict) -> dict | str | None:
    """Extrahiert mainsnak-datavalue eines Statements."""
    return claim.get("mainsnak", {}).get("datavalue", {}).get("value")


def _is_current(claim: dict) -> bool:
    """True wenn Statement KEINEN qualifier P582 (end_time) hat."""
    return "P582" not in (claim.get("qualifiers") or {})


def _item_id_of(claim: dict) -> str | None:
    """Aus einem item-typed Statement die referenzierte Q-ID ziehen."""
    val = _statement_value(claim)
    if isinstance(val, dict) and val.get("entity-type") == "item":
        return val.get("id")
    return None


def _string_of(claim: dict) -> str | None:
    val = _statement_value(claim)
    if isinstance(val, str):
        return val
    return None


def _pick_label(entity: dict, languages: tuple[str, ...] = ("de", "en")) -> str | None:
    labels = entity.get("labels", {})
    for lg in languages:
        if lg in labels:
            return labels[lg].get("value")
    if labels:
        return next(iter(labels.values())).get("value")
    return None


def _pick_description(entity: dict, languages: tuple[str, ...] = ("de", "en")) -> str | None:
    descs = entity.get("descriptions", {})
    for lg in languages:
        if lg in descs:
            return descs[lg].get("value")
    return None


def _pick_wikipedia_url(entity: dict) -> str | None:
    sitelinks = entity.get("sitelinks", {})
    for code in ("dewiki", "enwiki"):
        link = sitelinks.get(code)
        if link and link.get("url"):
            return link["url"]
        if link and link.get("title"):
            wiki = "de" if code == "dewiki" else "en"
            slug = link["title"].replace(" ", "_")
            return f"https://{wiki}.wikipedia.org/wiki/{slug}"
    return None


async def _resolve_item_labels(qids: list[str]) -> dict[str, str]:
    """Batch-Lookup für Q-IDs → Labels (de bevorzugt)."""
    unknown = [q for q in qids if q and q not in _LABEL_CACHE]
    if not unknown:
        return {q: _LABEL_CACHE[q] for q in qids if q in _LABEL_CACHE}
    # API erlaubt bis zu 50 IDs pro Call
    for i in range(0, len(unknown), 50):
        chunk = unknown[i : i + 50]
        data = await _wb_get({
            "action": "wbgetentities",
            "ids": "|".join(chunk),
            "props": "labels",
            "languages": "de|en",
            "format": "json",
        })
        if not data:
            break
        for qid, ent in (data.get("entities") or {}).items():
            label = _pick_label(ent)
            if label:
                _LABEL_CACHE[qid] = label
    return {q: _LABEL_CACHE[q] for q in qids if q in _LABEL_CACHE}


def _filter_human(entity: dict) -> bool:
    """P31 = Q5 (Mensch)? Pre-Filter B1."""
    for claim in (entity.get("claims") or {}).get("P31", []):
        if _item_id_of(claim) == "Q5":
            return True
    return False


def _extract_hit(entity: dict, label_map: dict[str, str]) -> WikidataPersonHit | None:
    qid = entity.get("id")
    if not qid:
        return None
    claims = entity.get("claims") or {}

    # P570 — Pre-Filter Deceased
    is_deceased = bool(claims.get("P570"))

    # P39 — aktuelle Position (ohne P582)
    current_position_qid: str | None = None
    for stmt in claims.get("P39", []):
        if _is_current(stmt):
            current_position_qid = _item_id_of(stmt)
            if current_position_qid:
                break
    # P106 — Occupation (Fallback)
    occupation_qid: str | None = None
    for stmt in claims.get("P106", []):
        occupation_qid = _item_id_of(stmt)
        if occupation_qid:
            break
    # P108 — Employer
    employer_qid: str | None = None
    for stmt in claims.get("P108", []):
        if _is_current(stmt):
            employer_qid = _item_id_of(stmt)
            if employer_qid:
                break

    # Social/Web (Literal-Strings)
    linkedin_id = None
    for stmt in claims.get("P6634", []):
        linkedin_id = _string_of(stmt) or linkedin_id
    twitter_handle = None
    for stmt in claims.get("P2002", []):
        twitter_handle = _string_of(stmt) or twitter_handle
    facebook_id = None
    for stmt in claims.get("P4003", []):
        facebook_id = _string_of(stmt) or facebook_id
    website_url = None
    for stmt in claims.get("P856", []):
        website_url = _string_of(stmt) or website_url

    # P569 date of birth — time-stamp extrahieren
    dob: str | None = None
    for stmt in claims.get("P569", []):
        v = _statement_value(stmt)
        if isinstance(v, dict) and v.get("time"):
            dob = v["time"]
            break

    return WikidataPersonHit(
        qid=qid,
        label=_pick_label(entity) or qid,
        description=_pick_description(entity),
        occupation=label_map.get(occupation_qid) if occupation_qid else None,
        current_position=label_map.get(current_position_qid) if current_position_qid else None,
        current_employer=label_map.get(employer_qid) if employer_qid else None,
        employer_qid=employer_qid,
        linkedin_id=linkedin_id,
        twitter_handle=twitter_handle,
        facebook_id=facebook_id,
        website_url=website_url,
        wikipedia_url=_pick_wikipedia_url(entity),
        date_of_birth=dob,
        is_deceased=is_deceased,
    )


async def lookup_wikidata_person(
    *,
    full_name: str,
    company_hint: str | None = None,
) -> list[WikidataPersonHit]:
    """2-Stufen-Lookup: wbsearchentities → QIDs → wbgetentities → Properties.

    WDQS-Outage-robust: nutzt KEINE SPARQL.
    """
    name = (full_name or "").strip()
    if not name:
        return []
    cache_key = name.casefold()
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    qids = await _resolve_qids(name)
    if not qids:
        _CACHE[cache_key] = []
        return []

    data = await _wb_get({
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "props": "claims|labels|descriptions|sitelinks",
        "languages": "de|en",
        "sitefilter": "dewiki|enwiki",
        "format": "json",
    })
    if not data:
        return []

    entities = data.get("entities") or {}
    # B1 Pre-Filter: nur Menschen (Q5)
    human_entities = {qid: ent for qid, ent in entities.items() if _filter_human(ent)}
    if not human_entities:
        _CACHE[cache_key] = []
        return []

    # Item-IDs sammeln (P39/P106/P108) für Label-Resolution
    ref_qids: set[str] = set()
    for ent in human_entities.values():
        claims = ent.get("claims") or {}
        for prop in ("P39", "P106", "P108"):
            for stmt in claims.get(prop, []):
                qid = _item_id_of(stmt)
                if qid:
                    ref_qids.add(qid)
    label_map = await _resolve_item_labels(sorted(ref_qids)) if ref_qids else {}

    hits: list[WikidataPersonHit] = []
    for ent in human_entities.values():
        h = _extract_hit(ent, label_map)
        if h and not h.is_deceased:
            hits.append(h)

    if company_hint:
        ch = company_hint.casefold().strip()
        hits.sort(
            key=lambda h: bool(h.current_employer and ch in h.current_employer.casefold()),
            reverse=True,
        )
    _CACHE[cache_key] = hits
    return hits


def linkedin_url(handle_or_id: str | None) -> str | None:
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


def facebook_url(handle_or_id: str | None) -> str | None:
    if not handle_or_id:
        return None
    if handle_or_id.startswith("http"):
        return handle_or_id
    return f"https://www.facebook.com/{handle_or_id.lstrip('@')}"
