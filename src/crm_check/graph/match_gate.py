"""Identity-Match-Gate gegen Surface-Match-False-Positives.

Implementiert die Vault-Architektur-Regel aus
`Konzepte/AI Newsroom/Entity Intelligence – Wissenschaftliche Fundierung.md`
§B6 (Record Linkage):

> Merge-Regel: NUR bei Vorname + Nachname Match ODER gleicher Wikidata-QID.
> NIEMALS nur nach Nachname (Lesson learned: „Patrick Schnieder" ≠ „Gordon
> Schnieder")

Pendant-Vorfall 2026-06-29: CRM hatte "Frau Ulrike Pieper (Bahlsen)", Lookup
matched auf "Ulrich Pieper (Lobbyregister Vorstand)" — verschiedene Personen,
verschiedenes Geschlecht, verschiedene Firma. Gate verhindert das.

API:
    passes_identity_gate(crm_first, crm_last, crm_company,
                        src_first, src_last, src_org) -> (bool, reason)

Determinismus:
- Keine externen Calls, keine ML, kein LLM — alles deterministisch + auditierbar.
- Aliasen-Tabelle (Sören/Soeren, Christof/Christoph, …) statisch.
- Gender-Heuristik nur als ZUSÄTZLICHER Reject-Grund, nie als Match-Beweis.
- Threshold-Logik: Last-Name-only PASS nur bei explizit unbekanntem Source-Vornamen
  UND Company-Match — sonst REJECT.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# Deutsche Vornamen-Aliases (Variant gleich Person)
_FIRST_NAME_ALIASES: dict[str, set[str]] = {
    "soeren": {"sören", "soeren", "soren"},
    "joerg": {"jörg", "joerg", "jorg"},
    "bjoern": {"björn", "bjoern", "bjorn"},
    "thoma": {"thomas", "tom"},
    "christof": {"christoph", "christof"},
    "stefan": {"stephan", "stefan"},
    "frederic": {"frédéric", "frederic", "frédéric"},
    "gerard": {"gérard", "gerard"},
    "andreas": {"andre", "andré", "andreas"},
    "michael": {"michael", "mike", "micha"},
    "alexander": {"alexander", "alex", "sascha", "sasha"},
    "katharina": {"katharina", "katja", "kati"},
    "elisabeth": {"elisabeth", "lisa", "elke"},
    "johannes": {"johannes", "hannes", "jan", "johann"},
    "wolfgang": {"wolfgang", "wolf"},
    "friedrich": {"friedrich", "fritz", "fred"},
    "ulrich": {"ulrich", "uli"},
}

# Gender-Heuristik — vorsichtige, eindeutige Listen (deutsche Vornamen).
# Nur eindeutig-männliche bzw eindeutig-weibliche. Unisex/ambig wird "unknown"
# und passiert das Gate (Gender-Mismatch reject feuert NUR bei eindeutig
# konträren Belegen wie Ulrike♀ vs Ulrich♂).
_MALE_FIRST_NAMES: frozenset[str] = frozenset({
    "alexander", "andreas", "andre", "andré", "anton", "bastian", "bernd",
    "bernhard", "björn", "bjoern", "carsten", "christian", "christof",
    "christoph", "claus", "daniel", "david", "detlef", "dieter", "dirk",
    "dominik", "eberhard", "eckhard", "erich", "erik", "ernst", "felix",
    "florian", "frank", "franz", "frédéric", "frederic", "friedrich",
    "fritz", "gabriel", "georg", "gerald", "gerd", "gérard", "gerard",
    "gerhard", "gordon", "günter", "guenter", "gunnar", "gunter", "hannes",
    "hans", "harald", "hartmut", "heiko", "heinrich", "heinz", "helmut",
    "henning", "henrik", "herbert", "hermann", "holger", "horst", "ingo",
    "jan", "joachim", "johannes", "johann", "jörg", "joerg", "josef", "julian",
    "jürgen", "juergen", "karl", "klaus", "konrad", "kurt", "lars", "leon",
    "lothar", "lukas", "ludwig", "manfred", "marc", "marcel", "marco", "marcus",
    "mario", "markus", "martin", "matthias", "max", "maximilian", "michael",
    "mike", "norbert", "oliver", "olaf", "oskar", "otto", "patrick", "paul",
    "peter", "philipp", "rainer", "ralf", "reinhard", "reiner", "richard",
    "robert", "rolf", "rüdiger", "ruediger", "rudolf", "sebastian", "siegfried",
    "simon", "sören", "soeren", "stefan", "stephan", "sven", "thomas", "tobias",
    "tom", "udo", "ulf", "ulrich", "uli", "uwe", "viktor", "volker", "walter",
    "werner", "willi", "wilhelm", "wolf", "wolfgang", "wolfram",
})

_FEMALE_FIRST_NAMES: frozenset[str] = frozenset({
    "alexandra", "andrea", "angela", "angelika", "anja", "anke", "anna",
    "anne", "annette", "anni", "antje", "astrid", "barbara", "beate", "bettina",
    "birgit", "brigitte", "carmen", "carola", "carolin", "caroline", "christa",
    "christel", "christiane", "christina", "christine", "claudia", "cornelia",
    "dagmar", "daniela", "diana", "doris", "edith", "eleonore", "elfriede",
    "elisabeth", "elke", "elsa", "emma", "erika", "eva", "franziska", "gabi",
    "gabriele", "gerda", "gertrud", "gisela", "gudrun", "hanna", "hannelore",
    "heide", "heidi", "heike", "helga", "helene", "hilde", "ilse", "ines",
    "inga", "inge", "ingrid", "irene", "iris", "jana", "janine", "jasmin",
    "jenny", "jessica", "johanna", "judith", "julia", "juliane", "jutta",
    "karin", "katharina", "kathrin", "katja", "kerstin", "klara", "kristin",
    "laura", "lea", "lena", "lina", "linda", "lisa", "lotte", "lydia",
    "maja", "manuela", "margarete", "margit", "maria", "marianne", "marie",
    "marina", "marion", "marlene", "martha", "martina", "melanie", "michaela",
    "monika", "nadine", "natalie", "natascha", "nicole", "nina", "olga",
    "patrizia", "petra", "renate", "rita", "rosa", "rose", "rosemarie",
    "sabine", "sandra", "sarah", "silvia", "simone", "sofia", "sonja", "sophie",
    "stefanie", "stephanie", "susanne", "tamara", "tanja", "theresa", "ulla",
    "ulrike", "ursula", "uta", "ute", "vanessa", "vera", "verena", "viktoria",
    "waltraud", "yvonne",
})


_ORG_SUFFIX_RE = re.compile(
    r"\b(gmbh|ag|kgaa|kg|se|ohg|gbr|e\.?v\.?|mbh|ug|co\.?|& co\.?|holding|group|gruppe|inc\.?|ltd\.?|llc)\b",
    re.IGNORECASE,
)


_UMLAUT_MAP = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
})


def _norm_token(s: str) -> str:
    """Lowercased + DIN-5007-2 Umlaut-Expansion (ä→ae, …) + diacritic-fold."""
    if not s:
        return ""
    expanded = s.translate(_UMLAUT_MAP)
    nkfd = unicodedata.normalize("NFKD", expanded)
    ascii_ = "".join(ch for ch in nkfd if not unicodedata.combining(ch))
    return re.sub(r"[^\w\s-]", " ", ascii_).strip().casefold()


def _norm_first(s: str) -> str:
    return _norm_token(s).split()[0] if s and s.strip() else ""


def _norm_last(s: str) -> str:
    return _norm_token(s).split()[-1] if s and s.strip() else ""


def _norm_org(s: str | None) -> str:
    if not s:
        return ""
    cleaned = _ORG_SUFFIX_RE.sub("", s)
    return re.sub(r"\s+", " ", _norm_token(cleaned)).strip()


def _alias_class(first: str) -> str:
    """Liefert die kanonische Alias-Klasse (Schlüssel) oder den Vornamen selbst."""
    n = _norm_first(first)
    if not n:
        return ""
    for key, members in _FIRST_NAME_ALIASES.items():
        if n in {_norm_token(m) for m in members}:
            return key
    return n


def _gender(first: str) -> str:
    n = _norm_first(first)
    if n in _MALE_FIRST_NAMES:
        return "m"
    if n in _FEMALE_FIRST_NAMES:
        return "f"
    return "?"


def _orgs_overlap(a: str | None, b: str | None) -> bool:
    """Token-Overlap ≥ 50 % nach Suffix-Strip → org-match."""
    na, nb = _norm_org(a), _norm_org(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return False
    overlap = len(ta & tb)
    return overlap / max(1, min(len(ta), len(tb))) >= 0.5


@dataclass
class GateDecision:
    """Audit-Eintrag pro Gate-Aufruf."""
    accepted: bool
    reason: str           # human-readable
    rule: str             # eine der hartcodierten Regel-IDs


def passes_identity_gate(
    *,
    crm_first: str,
    crm_last: str,
    crm_company: str | None,
    src_first: str | None,
    src_last: str,
    src_org: str | None,
) -> GateDecision:
    """Strenges Identity-Gate.

    Pflicht: Last-Name match. Dann eine der zwei Schienen:

    1. Vorname bekannt auf beiden Seiten:
       - Alias-Klasse gleich → PASS
       - Alias-Klasse verschieden + Gender-Mismatch (z.B. Ulrike♀/Ulrich♂)
         → REJECT (rule=R1_gender_mismatch)
       - Alias-Klasse verschieden, kein Gender-Mismatch → REJECT
         (rule=R2_firstname_mismatch)

    2. Vorname-Quelle unbekannt (None/leer):
       - Company-Token-Overlap ≥ 50 % → PASS (rule=R3_company_anchor)
       - sonst REJECT (rule=R4_no_anchor)
    """
    n_crm_last = _norm_last(crm_last)
    n_src_last = _norm_last(src_last)

    if not n_crm_last or not n_src_last:
        return GateDecision(False, "missing last-name on one side", "R0_missing_last_name")

    if n_crm_last != n_src_last:
        return GateDecision(False, f"last-name mismatch ({n_crm_last}≠{n_src_last})", "R0_last_name_mismatch")

    cf = _norm_first(crm_first)
    sf = _norm_first(src_first) if src_first else ""

    if sf:
        # Beide Seiten haben Vornamen
        if _alias_class(cf) == _alias_class(sf):
            return GateDecision(True, f"first-name alias-match ({cf}={sf})", "R5_firstname_match")
        # divergente Vornamen — prüfe Gender
        gc, gs = _gender(cf), _gender(sf)
        if gc and gs and gc != "?" and gs != "?" and gc != gs:
            return GateDecision(
                False,
                f"gender mismatch: CRM {cf}({gc}) vs source {sf}({gs})",
                "R1_gender_mismatch",
            )
        return GateDecision(
            False,
            f"first-name divergent: {cf!r}≠{sf!r}",
            "R2_firstname_mismatch",
        )

    # Quelle hat keinen Vornamen → braucht Company-Anker
    if _orgs_overlap(crm_company, src_org):
        return GateDecision(
            True,
            f"company anchor ({crm_company!r}≈{src_org!r}); source first-name unknown",
            "R3_company_anchor",
        )
    return GateDecision(
        False,
        f"no anchor: source first-name unknown AND company mismatch (crm={crm_company!r}, src={src_org!r})",
        "R4_no_anchor",
    )
