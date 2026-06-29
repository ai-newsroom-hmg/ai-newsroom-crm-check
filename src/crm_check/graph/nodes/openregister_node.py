"""OpenRegister-Node — Handelsregister-Validierung via offizielles SDK.

Nutzt `openregister-sdk` (PyPI), das die `api.openregister.de`-API abdeckt:
- `client.search.find_person_v1(query=...)` — direkter Person-Search
- `client.search.find_companies_v1(query=...)` — Company-Search
- `client.company.get_owners_v1(id)` — Officers (GF)
- `client.company.get_contact_v0(id)` — Adresse

Wenn OPENREGISTER_API_KEY fehlt → Node liefert leere Liste (graceful).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class OpenRegisterOfficer(BaseModel):
    name: str
    role: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    dismissed: bool = False


class OpenRegisterPersonHit(BaseModel):
    """Direkter find_person_v1-Treffer."""

    person_id: str | None = None
    full_name: str
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: str | None = None
    city: str | None = None
    active: bool | None = None
    associations: list[dict[str, Any]] = Field(default_factory=list)
    score: float | None = None


class OpenRegisterCandidate(BaseModel):
    company_id: str
    company_name: str
    registered_address: str | None = None
    register_type: str | None = None
    register_number: str | None = None
    current_status: str | None = None
    officers: list[OpenRegisterOfficer] = Field(default_factory=list)
    person_match: OpenRegisterOfficer | None = None
    address_match: bool = False


def _api_key_present() -> bool:
    return bool(os.environ.get("OPENREGISTER_API_KEY", "").strip())


async def lookup_openregister_person(
    *,
    full_name: str,
    city: str | None = None,
) -> list[OpenRegisterPersonHit]:
    """Person-Direct-Search — Mittelstand-Hebel ohne erst die Firma zu suchen."""
    if not _api_key_present() or not full_name.strip():
        return []
    try:
        from openregister import AsyncOpenregister
    except ImportError:
        log.warning("openregister-sdk nicht installiert")
        return []

    client = AsyncOpenregister(api_key=os.environ["OPENREGISTER_API_KEY"])
    filters: list[dict[str, Any]] = []
    if city:
        filters.append({"field": "city", "value": city})
    try:
        resp = await client.search.find_person_v1(
            query={"value": full_name.strip()},
            filters=filters or [{"field": "active", "value": "true"}],
            pagination={"size": 5},
        )
    except Exception as e:
        log.warning(f"openregister find_person '{full_name}': {e}")
        return []
    finally:
        try:
            await client.close()
        except Exception:
            pass

    hits: list[OpenRegisterPersonHit] = []
    results = getattr(resp, "results", None) or getattr(resp, "items", None) or []
    for r in results:
        rd = r.model_dump() if hasattr(r, "model_dump") else dict(r)
        hits.append(OpenRegisterPersonHit(
            person_id=rd.get("id") or rd.get("person_id"),
            full_name=rd.get("name") or " ".join(filter(None, [rd.get("first_name"), rd.get("last_name")])) or full_name,
            first_name=rd.get("first_name"),
            last_name=rd.get("last_name"),
            date_of_birth=rd.get("date_of_birth"),
            city=rd.get("city"),
            active=rd.get("active"),
            associations=rd.get("associations") or [],
            score=rd.get("score"),
        ))
    return hits


async def lookup_openregister_company(
    *,
    company: str,
    person_name: str | None = None,
    crm_address: str | None = None,
) -> list[OpenRegisterCandidate]:
    """Company-Search + Officer-Resolution + Person-Match."""
    if not _api_key_present() or not company.strip():
        return []
    try:
        from openregister import AsyncOpenregister
    except ImportError:
        return []

    client = AsyncOpenregister(api_key=os.environ["OPENREGISTER_API_KEY"])
    cands: list[OpenRegisterCandidate] = []
    try:
        co_resp = await client.search.find_companies_v1(
            query={"value": company.strip()},
            pagination={"size": 3},
        )
        results = getattr(co_resp, "results", None) or getattr(co_resp, "items", None) or []
        for r in results[:3]:
            rd = r.model_dump() if hasattr(r, "model_dump") else dict(r)
            cid = rd.get("id") or rd.get("company_id")
            if not cid:
                continue
            cand = OpenRegisterCandidate(
                company_id=cid,
                company_name=rd.get("name") or rd.get("company_name") or company,
                registered_address=rd.get("registered_address"),
                register_type=rd.get("register_type"),
                register_number=rd.get("register_number"),
                current_status=rd.get("current_status"),
            )
            # Officers
            try:
                ow_resp = await client.company.get_owners_v1(cid)
                ow_data = ow_resp.model_dump() if hasattr(ow_resp, "model_dump") else dict(ow_resp)
                for o in (ow_data.get("officers") or ow_data.get("owners") or []):
                    cand.officers.append(OpenRegisterOfficer(
                        name=o.get("name", ""),
                        role=o.get("position") or o.get("role"),
                        start_date=o.get("start_date"),
                        end_date=o.get("end_date"),
                        dismissed=bool(o.get("dismissed")),
                    ))
            except Exception as e:
                log.warning(f"openregister owners {cid}: {e}")

            # Person-Match
            if person_name:
                pn = person_name.casefold().strip()
                for o in cand.officers:
                    on = o.name.casefold().strip()
                    if pn in on or on in pn:
                        cand.person_match = o
                        break

            # Address-Match (PLZ + Stadt)
            if crm_address and cand.registered_address:
                addr_lc = cand.registered_address.casefold()
                tokens = [t for t in crm_address.split() if len(t) >= 2]
                plz_match = any(
                    t in cand.registered_address for t in tokens
                    if len(t) == 5 and t.isdigit()
                )
                city_tokens = [
                    t.casefold() for t in tokens
                    if not (len(t) == 5 and t.isdigit()) and len(t) >= 3
                ]
                city_match = any(t in addr_lc for t in city_tokens)
                cand.address_match = bool(plz_match and city_match) or bool(plz_match)

            cands.append(cand)
    except Exception as e:
        log.warning(f"openregister find_companies '{company}': {e}")
    finally:
        try:
            await client.close()
        except Exception:
            pass

    return cands


# Kompatibilitäts-Wrapper für lookup_nodes.py
async def lookup_openregister(
    *,
    company: str | None,
    person_name: str | None = None,
    crm_address: str | None = None,
    city: str | None = None,
) -> dict[str, Any]:
    """Kombi-Lookup: erst Person, falls 0 dann Company-Fallback."""
    person_hits: list[OpenRegisterPersonHit] = []
    company_hits: list[OpenRegisterCandidate] = []
    if person_name:
        person_hits = await lookup_openregister_person(full_name=person_name, city=city)
    if company:
        company_hits = await lookup_openregister_company(
            company=company, person_name=person_name, crm_address=crm_address
        )
    return {"persons": person_hits, "companies": company_hits}
