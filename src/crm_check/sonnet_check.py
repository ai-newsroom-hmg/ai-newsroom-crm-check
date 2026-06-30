"""Sonnet 4.6 via OpenRouter (:online) — Kollegen-Pattern im crm-check.

User-Direktive 2026-06-30:
1. Pivot von Llama-3.3:70b weg. Llama Cutoff Q4-2024 kennt das Kabinett Merz
   (Feb 2025) nicht.
2. „nimm den gleichen Key, den auch der chat im ai newsroom nimmt" → OpenRouter.
   Der chat-agent ruft `anthropic/claude-sonnet-4-6` ueber OpenRouter's OpenAI-
   kompatible API auf.

OpenRouter `:online`-Suffix aktiviert built-in Web-Search (Brave/Exa-Backend) —
kein eigener Tool-Loop noetig. Cost: ~$4 pro 1000 web-results plus Token-Cost.

Verwirft 8-Stage-LangGraph (Wikidata, OpenRegister, KG-Lobby, NI, correlate,
verify, reason) — alle hatten Llama als Brain.

DSGVO: nur Name + Position + Firma raus an OpenRouter→Anthropic. KEINE Adresse.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from crm_check.parser import CrmContact

log = logging.getLogger(__name__)

DEFAULT_MODEL = "anthropic/claude-sonnet-4-6:online"  # :online = OpenRouter built-in web-search
DEFAULT_BATCH_SIZE = 5    # kleiner bei OpenRouter, mehr Web-Quality pro Person
DEFAULT_MAX_PARALLEL = 6  # Sonnet via OpenRouter rate-limited; konservativ starten
DEFAULT_WEB_MAX_RESULTS = 8   # pro Anfrage, gefuettert via extra_body plugin
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"


class SonnetVerdict(BaseModel):
    row_idx: int
    aktuell: bool | None = None
    bemerkung: str = ""
    konfidenz: float = 0.0
    quellen: list[str] = Field(default_factory=list)
    linkedin: str | None = None
    neue_position: str | None = None
    neue_firma: str | None = None
    raw_text: str | None = None


SYSTEM_PROMPT = """Du bist Aktualitaets-Pruefer fuer deutsche B2B-CRM-Mailing-Listen.
Stand: Juni 2026. Du hast Web-Zugriff (online-mode) — NUTZE IHN AKTIV.

Eingabe: JSON-Array von Personen mit row_idx, name, position, firma.

POLITISCHER KONTEXT (kritisch fuer Recall):
- 21. Wahlperiode des Bundestags seit Februar 2025 (Neuwahl nach Ampel-Bruch).
- Kabinett Merz (CDU/SPD) seit Mai 2025: Wadephul=AA, Klingbeil=Finanzen, Bas=Arbeit,
  Reiche=Wirtschaft, Schnieder=Verkehr, Dobrindt=Inneres, Hubig=Justiz, Frei=Kanzleramt.
- Alte Ampel-Minister (Heil, Lauterbach, Faeser-Innen, Habeck) sind NICHT mehr Minister.
- Laschet/Mützenich = nicht mehr in zentralen Rollen; Nouripour = Vizepraesident BT.

Fuer JEDE Person mit aggressivem Web-Search:
1. Suche "<name> <firma>" — Firmen-Website, LinkedIn, Pressemitteilungen.
2. Bei MdB-Verdacht: suche "<name> Bundestag" und pruefe 21. WP.
3. Bei Verdacht auf Wechsel: suche "<name> neuer Posten", "<name> wechselt".
4. Suche immer auch "<name> linkedin" — extrahiere die linkedin.com/in/-URL.
5. Min. 2 Quellen ansehen, bevor du „aktuell=true" sagst.

OUTPUT-KLASSEN (so wie der Referenz-Standard 27.06.2026):
- 🔴 VERALTET: Belege fuer Wechsel/Ausscheiden → aktuell=false, neue_position/firma + Datum
- 🟡 UNGENAU: Titel inhaltlich richtig aber Detail veraltet → aktuell=false, Bemerkung nennt Diff
- ✅ BESTAETIGT: Mind. 2 Quellen bestaetigen aktuelle Position → aktuell=true, konfidenz≥0.85
- ⚪ NICHT VERIFIZIERT: <2 Quellen oder keine eindeutigen Belege → aktuell=null, konfidenz<0.5

REGELN:
- KEINE Halluzinationen. Wenn die Suche nichts Belastbares findet → ⚪.
- Quellen-URLs als echte URLs angeben (nicht "siehe Wikipedia" sondern die URL).
- bemerkung in Deutsch, ein Satz, mit Datum wenn bekannt (z.B. "Seit 6. Mai 2025 Bundesaussenminister").
- linkedin: URL oder null. Niemals erfinden.
- konfidenz: 0.9+ = mehrere starke Quellen; 0.7 = 1 starke Quelle (z.B. Firmen-Website oder bundestag.de); 0.5 = indirekt; <0.5 = unsicher.

ANTWORTFORMAT — strikt valides JSON, KEIN Markdown ausserhalb der Tags:

<verdicts>
[
  {
    "row_idx": <int>,
    "aktuell": <true|false|null>,
    "bemerkung": "<ein Satz Deutsch, mit Datum wenn bekannt>",
    "konfidenz": <float 0-1>,
    "quellen": ["<https://...>", "<https://...>"],
    "linkedin": "<https://www.linkedin.com/in/...|null>",
    "neue_position": "<str|null>",
    "neue_firma": "<str|null>"
  }
]
</verdicts>

GENAU ein Eintrag pro row_idx aus der Eingabe. Reihenfolge egal.
"""


def _build_input(rows: Sequence[CrmContact]) -> str:
    items = [
        {
            "row_idx": r.row_idx,
            "name": r.name_only or r.salutation_name or "",
            "position": r.position or "",
            "firma": r.company or "",
        }
        for r in rows
    ]
    return json.dumps(items, ensure_ascii=False, indent=2)


def _collect_text(content_blocks) -> str:
    out = []
    for block in content_blocks:
        if getattr(block, "type", None) == "text":
            out.append(getattr(block, "text", "") or "")
    return "".join(out)


def _extract_verdicts(text: str, expected_idx: list[int]) -> list[SonnetVerdict]:
    """Parst <verdicts>JSON</verdicts>. Tolerant gegen Whitespace und fehlende Tags."""
    m = re.search(r"<verdicts>\s*(\[.*?\])\s*</verdicts>", text, re.DOTALL)
    if not m:
        m = re.search(r"(\[\s*\{[\s\S]*?\"row_idx\"[\s\S]*?\}\s*\])", text)
    if not m:
        log.warning("Sonnet response hat kein verdicts-JSON. Text (300): %s", text[:300])
        return [
            SonnetVerdict(
                row_idx=i,
                bemerkung="Parser konnte Sonnet-Antwort nicht lesen",
                raw_text=text[:1000],
            )
            for i in expected_idx
        ]
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        log.warning("Sonnet JSON parse failed: %s. Block: %s", e, m.group(1)[:300])
        return [
            SonnetVerdict(row_idx=i, bemerkung=f"JSON-Decode-Fehler: {e}", raw_text=m.group(1)[:1000])
            for i in expected_idx
        ]

    verdicts: list[SonnetVerdict] = []
    seen: set[int] = set()
    for item in items:
        try:
            v = SonnetVerdict(**item)
        except Exception as e:
            log.warning("Verdict-Item invalid: %s — %s", e, item)
            continue
        verdicts.append(v)
        seen.add(v.row_idx)
    for i in expected_idx:
        if i not in seen:
            verdicts.append(
                SonnetVerdict(row_idx=i, bemerkung="row_idx in Sonnet-Antwort fehlend")
            )
    return sorted(verdicts, key=lambda v: v.row_idx)


async def check_batch(
    client,
    rows: Sequence[CrmContact],
    *,
    model: str = DEFAULT_MODEL,
    web_max_results: int = DEFAULT_WEB_MAX_RESULTS,
) -> list[SonnetVerdict]:
    """Ein OpenRouter-Call fuer ein Batch von Personen.

    Nutzt `:online`-Suffix am Model fuer OpenRouter built-in web-search. Plugin-
    Config via extra_body steuert max_results pro Suche.
    """
    if not rows:
        return []
    expected = [r.row_idx for r in rows]
    user_msg = _build_input(rows)
    log.info("OpenRouter-Sonnet batch start: %d rows, idxs=%s", len(rows), expected)

    try:
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=16000,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            extra_body={
                "plugins": [{"id": "web", "max_results": web_max_results}],
            },
        )
    except Exception as e:
        log.error(
            "OpenRouter API error (batch idxs=%s): %s: %r",
            expected, type(e).__name__, e,
        )
        return [
            SonnetVerdict(row_idx=i, bemerkung=f"OpenRouter API-Fehler: {type(e).__name__}: {e}")
            for i in expected
        ]

    if not resp.choices:
        return [SonnetVerdict(row_idx=i, bemerkung="OpenRouter: leere choices") for i in expected]

    msg = resp.choices[0].message
    final_text = msg.content or ""
    usage = getattr(resp, "usage", None)
    log.info(
        "OpenRouter batch done: idxs=%s, text_len=%d, usage=%s",
        expected, len(final_text), usage,
    )
    return _extract_verdicts(final_text, expected)


async def check_rows_parallel(
    rows: Sequence[CrmContact],
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    progress_cb=None,
) -> list[SonnetVerdict]:
    """Voll-Run via OpenRouter mit Semaphore-begrenzter Parallelitaet."""
    try:
        from openai import AsyncOpenAI
    except ImportError as e:
        raise RuntimeError(
            "openai-Package fehlt. Install: pip install 'openai>=1.50'"
        ) from e

    key = (
        api_key
        or os.environ.get("OPENROUTER_API_KEY", "").strip()
    )
    if not key:
        # Fallback: lese aus macOS-Keychain (Pattern wie ~/.zshrc)
        try:
            import subprocess
            key = subprocess.check_output(
                ["security", "find-generic-password", "-s", "OpenRouter", "-w"],
                text=True,
            ).strip()
        except Exception:
            key = ""
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY nicht gesetzt. "
            "Keychain: security find-generic-password -s 'OpenRouter' -w"
        )

    client = AsyncOpenAI(
        base_url=DEFAULT_OPENROUTER_BASE,
        api_key=key,
        default_headers={
            "HTTP-Referer": "https://github.com/ai-newsroom-hmg/ai-newsroom-crm-check",
            "X-Title": "ai-newsroom-crm-check",
        },
    )
    rows_list = list(rows)
    batches = [rows_list[i : i + batch_size] for i in range(0, len(rows_list), batch_size)]
    sem = asyncio.Semaphore(max_parallel)
    done_count = 0
    total = len(batches)

    async def _run(idx: int, batch: Sequence[CrmContact]) -> list[SonnetVerdict]:
        nonlocal done_count
        async with sem:
            try:
                result = await check_batch(client, batch, model=model)
            finally:
                done_count += 1
                if progress_cb:
                    progress_cb(done_count, total, idx)
        return result

    results = await asyncio.gather(*(_run(i, b) for i, b in enumerate(batches)))
    flat: list[SonnetVerdict] = []
    for r in results:
        flat.extend(r)
    return sorted(flat, key=lambda v: v.row_idx)
