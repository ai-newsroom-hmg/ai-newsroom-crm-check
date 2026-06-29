"""Parser-Tests gegen die Sample-Fixture (lokal, nicht im Repo)."""

from pathlib import Path

import pytest

from crm_check.parser import CrmContact, parse_excel

FIXTURE = Path(__file__).parent / "fixtures" / "sample_10.xlsx"


@pytest.fixture
def contacts() -> list[CrmContact]:
    if not FIXTURE.exists():
        pytest.skip(f"Sample fixture missing: {FIXTURE}")
    return list(parse_excel(FIXTURE))


def test_yields_ten_rows(contacts):
    assert len(contacts) == 10


def test_first_row_is_schwittay(contacts):
    c = contacts[0]
    assert c.row_idx == 2
    assert c.salutation_name == "Herr Frank Schwittay"
    assert c.name_only == "Frank Schwittay"
    assert c.position == "Geschäftsführer"
    assert c.company == "Trend Micro Deutschland GmbH"
    assert c.street == "Zeppelinstr. 1"
    assert c.zip_code == "85399"
    assert c.city == "Hallbergmoos"
    assert c.country == "DE"


def test_richter_keeps_diacritics(contacts):
    # Gérard Richter — accent must survive
    c = contacts[2]
    assert c.salutation_name == "Herr Gérard Richter"
    assert c.name_only == "Gérard Richter"
    assert "McKinsey" in c.company


def test_koenigsmarck_long_name(contacts):
    # "Timo Graf von Koenigsmarck" — multi-token, mit Adelstitel
    c = contacts[3]
    assert "Koenigsmarck" in c.name_only
    assert c.position == "Head of Public Sector"
    assert c.company == "Capgemini Deutschland GmbH"


def test_eberwein_doctor_title(contacts):
    # "Dr. Rolf Eberwein" — Titel im name_only
    c = contacts[4]
    assert c.salutation_name == "Herr Dr. Rolf Eberwein"
    assert c.name_only == "Dr. Rolf Eberwein"
    assert c.company == "KAESER KOMPRESSOREN GmbH"


def test_display_format(contacts):
    c = contacts[0]
    assert c.display == "Frank Schwittay — Geschäftsführer @ Trend Micro Deutschland GmbH"


def test_raw_preserves_all_columns(contacts):
    c = contacts[0]
    # Audit-Trail braucht alle Originalspalten
    assert "ID" in c.raw
    assert "Mailcode" in c.raw
    assert c.raw["ID"] == 642013
