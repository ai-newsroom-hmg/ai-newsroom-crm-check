"""hugoplus-Lookup (HB-CMS) — Pipeline-v2 Tier-2 Agenturmeldungen.

Quelle: huGO Plus API (hugoplus.handelsblatt.media), Reuters + dpa + dpa-afx.
HB/WiWo-Eigenmaterial wird serverseitig herausgefiltert (agentur_flag=T).
Kein per-Call-Cost — HMG-Bestandsabo, Auth via Username/Password aus
HUGOPLUS_USER + HUGOPLUS_PASS. Session-Cookie wird gecached.

Self-contained vendored Client — kein Dependency-Import aus ai-newsroom-chat-agent
(damit CRM-Check pip-installable bleibt).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

log = logging.getLogger(__name__)

BASE_URL = "https://hugoplus.handelsblatt.media"

_MANDANT_NAMES = {
    65: "Reuters", 66: "dpa", 67: "dpa-afx", 70: "dpa", 107: "dpa-afx",
}

# Per-User Session-Cache (synchron zwischen Calls innerhalb eines Prozesses)
_sessions: dict[str, dict[str, str]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock(user: str) -> asyncio.Lock:
    if user not in _locks:
        _locks[user] = asyncio.Lock()
    return _locks[user]


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</(?:p|div|h[1-6]|li|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    for old, new in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                      ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


async def _login(user: str, password: str) -> dict[str, str]:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cx:
        r = await cx.post(
            f"{BASE_URL}/auth/login",
            json={"username": user, "password": password},
            headers={"Content-Type": "application/json"},
        )
        if r.status_code != 200:
            raise RuntimeError(f"hugoplus login HTTP {r.status_code}")
        data = r.json()
        if not data.get("data", {}).get("authenticated"):
            raise RuntimeError("hugoplus login: authenticated=false")
        cookies = {c.name: c.value for c in cx.cookies.jar}
        if "huGOPlusSession" not in cookies and "session" not in cookies:
            raise RuntimeError(f"hugoplus: no session cookie ({list(cookies.keys())})")
        return cookies


async def _ensure_session(user: str, password: str) -> dict[str, str]:
    if user in _sessions:
        return _sessions[user]
    async with _lock(user):
        if user in _sessions:
            return _sessions[user]
        _sessions[user] = await _login(user, password)
        return _sessions[user]


def _cookie_str(c: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in c.items())


class HugoplusHit(BaseModel):
    """Ein Treffer aus huGO Plus."""

    doc_id: str | None = None
    media_id: int | str | None = None
    headline: str
    snippet: str | None = None
    source: str | None = None        # Reuters / dpa / dpa-afx
    insert_ts: str | None = None     # ISO-Timestamp
    article_date: datetime | None = None
    author: str | None = None
    ressort: str | None = None
    word_count: int | None = None
    company_match: bool = False
    url: str | None = None           # huGO Plus interner Permalink


async def search_hugoplus(
    *,
    user: str,
    password: str,
    query: str,
    rows: int = 10,
    days_back: int = 365,
) -> list[HugoplusHit]:
    """Sucht Agenturmeldungen die ``query`` enthalten."""
    if not query.strip():
        return []
    cookies = await _ensure_session(user, password)

    facet_filter: list[dict] = [
        {"field": "agentur_flag", "operator": "OR", "type": "keyword", "value": ["T"]}
    ]
    if days_back > 0:
        from datetime import date, timedelta
        d_from = (date.today() - timedelta(days=days_back)).isoformat()
        facet_filter.append({
            "field": "insert_ts", "operator": "AND", "type": "date_range",
            "value": [f"{d_from}T00:00:00Z"],
        })

    payload = {
        "assetTypes": ["ART"],
        "queryFilter": [{
            "field": "freetext", "operator": "AND", "type": "freetext",
            "value": [query],
        }],
        "facet": [],
        "facetFilter": facet_filter,
        "rows": min(rows, 50),
        "sort": [{"field": "insert_ts", "order": "desc"}],
        "start": 0,
        "timeZone": "Europe/Berlin",
        "hl": {"resultExcerptLength": 0, "resultExcerptCount": 0,
                "resultHighlightPre": "", "resultHighlightPost": "",
                "resultExcerptFields": []},
    }

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cx:
        r = await cx.post(
            f"{BASE_URL}/api/search",
            json=payload,
            headers={"Cookie": _cookie_str(cookies),
                     "Content-Type": "application/json"},
        )
        if r.status_code == 401:
            _sessions.pop(user, None)
            cookies = await _ensure_session(user, password)
            r = await cx.post(
                f"{BASE_URL}/api/search",
                json=payload,
                headers={"Cookie": _cookie_str(cookies),
                         "Content-Type": "application/json"},
            )
        r.raise_for_status()
        data = r.json()

    raw = data.get("result", [])
    out: list[HugoplusHit] = []
    for art in raw:
        mandant_id = art.get("mandant_id")
        if mandant_id not in _MANDANT_NAMES:
            continue   # nur Agenturen, kein Eigenmaterial
        headline = art.get("ueberschrift_ctx") or ""
        if not headline:
            titelbereich = art.get("titelbereich") or []
            headline = titelbereich[0] if titelbereich else ""
        if not headline:
            continue
        grundtext = _strip_html(art.get("grundtext_ctx") or "")
        snippet = grundtext[:400] if grundtext else None
        ts = art.get("insert_ts") or ""
        try:
            ad = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
        except (ValueError, TypeError):
            ad = None
        out.append(HugoplusHit(
            doc_id=str(art.get("document_id") or ""),
            media_id=art.get("media_id"),
            headline=headline,
            snippet=snippet,
            source=_MANDANT_NAMES.get(mandant_id, ""),
            insert_ts=ts or None,
            article_date=ad,
            author=art.get("autor_ctx") or None,
            ressort=art.get("ressort_name") or None,
            word_count=art.get("wortanzahl_num"),
        ))
    return out


def _matches_company(text: str | None, company: str) -> bool:
    if not text or not company:
        return False
    c = company.casefold().strip()
    for suf in (" gmbh", " ag", " se", " kgaa", " mbh", " kg", " e.v.", " ev"):
        if c.endswith(suf):
            c = c[: -len(suf)].strip()
    return bool(c) and c in text.casefold()


def annotate_company_match(hits: list[HugoplusHit], company: str | None) -> list[HugoplusHit]:
    if not company:
        return hits
    for h in hits:
        h.company_match = _matches_company(h.headline, company) or \
                          _matches_company(h.snippet, company)
    hits.sort(key=lambda h: (h.company_match, h.article_date or datetime.min), reverse=True)
    return hits


def build_query(_: Any = None) -> tuple[str, Any]:
    """Test-Hook (kein SQL — Test prueft nur dass POST /api/search benutzt wird)."""
    return f"POST {BASE_URL}/api/search", None
