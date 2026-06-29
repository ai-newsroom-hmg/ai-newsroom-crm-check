"""Name-Normalisierung — Python-Spiegel der SQL-Funktion `kg.normalize_name`.

Source-of-truth ist `hmg-knowledge-graph/init-db/10-person-universe.sql:116`:

    CREATE OR REPLACE FUNCTION kg.normalize_name(name TEXT)
    RETURNS TEXT AS $$
    SELECT lower(regexp_replace(unaccent(coalesce(name, '')), '\\s+', ' ', 'g'));
    $$ LANGUAGE SQL IMMUTABLE;

Wir replizieren die Semantik in Python, damit lokale Tests + Pre-Filtering
identisch zur DB-Funktion arbeiten. Der finale Match läuft trotzdem über die
DB (Trigram-Index), aber das Pre-Filter spart Round-Trips bei klar leeren Namen.
"""

from __future__ import annotations

import re
import unicodedata

# Anreden + Titel die am Zeilenanfang gestrippt werden für den Match-Key.
# Die Lookup-Quellen (kg.lobby_persons.first_name, ni.entities.name) speichern
# Titel meist NICHT — daher beim Match strippen. Original-Spalte mit Titel
# bleibt im raw_row/salutation_name erhalten.
_SALUTATION_PATTERN = re.compile(
    r"^\s*("
    r"herr|frau|hr\.?|fr\.?|mr\.?|mrs\.?|ms\.?|mister|madam"
    r"|dr\.?|prof\.?|prof\.?\s*dr\.?|dipl\.?[-\s]?ing\.?|dipl\.?[-\s]?kfm\.?"
    r")\s+",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")


def _strip_accents(text: str) -> str:
    """Entfernt diakritische Zeichen — entspricht Postgres `unaccent`.

    NFD zerlegt 'é' in 'e' + combining-acute; filter erhält nur Basiszeichen.
    """
    normalized = unicodedata.normalize("NFD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def normalize_name(name: str | None) -> str:
    """Lowercase + accent-strip + whitespace-collapse. SQL-äquivalent.

    >>> normalize_name("Gérard Richter")
    'gerard richter'
    >>> normalize_name("Frank   Schwittay  ")
    'frank schwittay'
    >>> normalize_name(None)
    ''
    """
    if not name:
        return ""
    folded = _strip_accents(name)
    collapsed = _WHITESPACE_RE.sub(" ", folded).strip()
    return collapsed.lower()


def strip_salutation(name: str | None) -> str:
    """Strippt deutsche Anreden + akademische Titel iterativ am Anfang.

    Mehrfach (z.B. "Frau Dr. Prof. Müller" → "Müller"). Adelstitel (Graf von,
    Freiherr) bleiben — sie sind oft Teil des Nachnamens.

    >>> strip_salutation("Herr Frank Schwittay")
    'Frank Schwittay'
    >>> strip_salutation("Frau Dr. Maria Müller")
    'Maria Müller'
    >>> strip_salutation("Dr. Norbert Röttgen")
    'Norbert Röttgen'
    >>> strip_salutation("Timo Graf von Koenigsmarck")
    'Timo Graf von Koenigsmarck'
    """
    if not name:
        return ""
    prev = None
    cur = name
    while prev != cur:
        prev = cur
        cur = _SALUTATION_PATTERN.sub("", cur, count=1).strip()
    return cur


def name_for_matching(salutation_name: str | None) -> str:
    """Convenience: strip salutation, then normalize. Für DB-Trigram-Query."""
    return normalize_name(strip_salutation(salutation_name))
