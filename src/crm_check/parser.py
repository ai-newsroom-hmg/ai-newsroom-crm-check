"""Excel-Parser für CRM-Mailing-Listen im AD_D-Layout (20 Spalten)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import openpyxl
from pydantic import BaseModel, Field

from crm_check.normalize import strip_salutation


class CrmContact(BaseModel):
    """Eine Zeile aus der Mailingliste, semantisch interpretiert.

    Die echte Excel hat die strukturierten Felder ID/KZ/City/etc. plus
    den Adressblock AddrLine1..10, der nach Konvention so belegt ist:
      AddrLine1 = Name pur ("Frank Schwittay")
      AddrLine2 = Position/Funktion ("Geschäftsführer")
      AddrLine3 = Firma ("Trend Micro Deutschland GmbH")
      AddrLine4 = Straße ("Zeppelinstr. 1")
      AddrLine5 = PLZ Ort ("85399 Hallbergmoos")
      AddrLine6..10 = leer (oder Anhängsel wie Postfach)
    """

    row_idx: int
    raw: dict[str, Any] = Field(default_factory=dict)
    salutation_name: str = ""  # "Herr Frank Schwittay" aus FullPerson
    name_only: str = ""        # "Frank Schwittay" aus AddrLine1 oder gestrippt
    position: str = ""         # AddrLine2
    company: str = ""          # AddrLine3
    street: str = ""           # AddrLine4
    zip_city: str = ""         # AddrLine5
    city: str = ""             # ZipCode/City-Spalte (strukturiert)
    zip_code: str = ""         # ZipCode-Spalte
    country: str = ""          # CountryCode

    @property
    def display(self) -> str:
        return f"{self.name_only} — {self.position} @ {self.company}"


EXPECTED_HEADER = (
    "ID", "KZ", "AnmeldeCode", "Mailcode",
    "ZipCode", "City", "CountryCode",
    "FullPerson", "DearFullPerson", "LiebeAnrede",
    "AddrLine1", "AddrLine2", "AddrLine3", "AddrLine4", "AddrLine5",
    "AddrLine6", "AddrLine7", "AddrLine8", "AddrLine9", "AddrLine10",
)


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


_ONLY_SALUTATION_TOKENS = {
    "herr", "frau", "hr", "hr.", "fr", "fr.",
    "mr", "mr.", "mrs", "mrs.", "ms", "ms.",
    "dr", "dr.", "prof", "prof.",
}


def _is_only_salutation(s: str) -> bool:
    """True wenn der String NUR aus einer Anrede besteht (z.B. AddrLine1='Herr')."""
    return s.strip().lower() in _ONLY_SALUTATION_TOKENS


def _smart_name_only(addr_line_1: str, full_person: str) -> str:
    """Liefert den brauchbaren Klarnamen.

    Im echten AD_D-Layout ist AddrLine1 mal der ganze Name ("Frank Schwittay")
    und mal nur die Anrede ("Herr"). Wenn AddrLine1 leer ist oder nur eine
    Anrede enthält, fallback auf strip_salutation(FullPerson). Vorfall
    2026-06-29: R5 AddrLine1="Herr" + FullPerson="Herr Helmut Luksch" → ohne
    Fix steht `name_only="Herr"` im State und alle Lookups suchen nach "Herr".

    AddrLine1 mit substantiellem Inhalt (inkl. akademischen Titeln wie "Dr.")
    bleibt UNVERÄNDERT — der name_only-Slot transportiert auch Titel weiter.
    """
    if addr_line_1 and not _is_only_salutation(addr_line_1):
        return addr_line_1
    return strip_salutation(full_person).strip() if full_person else ""


def parse_excel(path: str | Path, sheet: str | None = None) -> Iterator[CrmContact]:
    """Iteriert über die Datenzeilen einer Mailing-Excel.

    :param path: Pfad zur .xlsx
    :param sheet: Sheet-Name; None = erstes Sheet
    :raises ValueError: wenn Header nicht zum erwarteten 20-Spalten-Layout passt
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.active

    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        return
    header_tuple = tuple(_coerce_str(h) for h in header)
    if header_tuple[: len(EXPECTED_HEADER)] != EXPECTED_HEADER:
        raise ValueError(
            "Unerwartetes Excel-Layout — Header weicht ab. "
            f"Erwartet: {EXPECTED_HEADER!r} Gefunden: {header_tuple!r}"
        )

    col = {name: idx for idx, name in enumerate(header_tuple)}

    for row_idx, row in enumerate(rows, start=2):
        if row is None or all(c is None for c in row):
            continue
        raw = {h: row[i] if i < len(row) else None for h, i in col.items()}

        salutation_name = _coerce_str(raw.get("FullPerson"))
        addr_line_1 = _coerce_str(raw.get("AddrLine1"))
        yield CrmContact(
            row_idx=row_idx,
            raw=raw,
            salutation_name=salutation_name,
            name_only=_smart_name_only(addr_line_1, salutation_name),
            position=_coerce_str(raw.get("AddrLine2")),
            company=_coerce_str(raw.get("AddrLine3")),
            street=_coerce_str(raw.get("AddrLine4")),
            zip_city=_coerce_str(raw.get("AddrLine5")),
            city=_coerce_str(raw.get("City")),
            zip_code=_coerce_str(raw.get("ZipCode")),
            country=_coerce_str(raw.get("CountryCode")),
        )

    wb.close()
