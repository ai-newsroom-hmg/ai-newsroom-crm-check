"""Normalisierungs-Tests — müssen 1:1 zur SQL-Funktion kg.normalize_name passen."""

import pytest

from crm_check.normalize import name_for_matching, normalize_name, strip_salutation


class TestNormalizeName:
    def test_simple(self):
        assert normalize_name("Frank Schwittay") == "frank schwittay"

    def test_accents_stripped(self):
        # Gérard → gerard (unaccent-Äquivalent)
        assert normalize_name("Gérard Richter") == "gerard richter"

    def test_umlaut_decomposes(self):
        # ä → a (unaccent strips combining diacritic)
        assert normalize_name("Müller") == "muller"

    def test_whitespace_collapsed(self):
        assert normalize_name("Frank   Schwittay  ") == "frank schwittay"

    def test_tab_and_newline(self):
        assert normalize_name("Frank\tSchwittay\n") == "frank schwittay"

    def test_none(self):
        assert normalize_name(None) == ""

    def test_empty(self):
        assert normalize_name("") == ""


class TestStripSalutation:
    def test_herr(self):
        assert strip_salutation("Herr Frank Schwittay") == "Frank Schwittay"

    def test_frau(self):
        assert strip_salutation("Frau Maria Müller") == "Maria Müller"

    def test_hr_abkuerzung(self):
        assert strip_salutation("Hr. Frank Schwittay") == "Frank Schwittay"

    def test_case_insensitive(self):
        assert strip_salutation("HERR Frank") == "Frank"
        assert strip_salutation("herr Frank") == "Frank"

    def test_title_stripped(self):
        # Akademische Titel werden gestrippt (Röttgen-Bug 2026-06-29): "Dr." wurde
        # sonst als first_name interpretiert und Trigram-Match auf "Dr Maria Müller"
        # findet nicht "Maria Müller" in kg.person_universe.
        assert strip_salutation("Frau Dr. Maria Müller") == "Maria Müller"
        assert strip_salutation("Herr Prof. Dr. Hans Meier") == "Hans Meier"
        assert strip_salutation("Dipl.-Ing. Anna Schulz") == "Anna Schulz"

    def test_adelstitel_survives(self):
        # Graf/von bleibt — KG-Datensatz hat ihn meist auch
        assert strip_salutation("Herr Timo Graf von Koenigsmarck") == "Timo Graf von Koenigsmarck"

    def test_no_salutation(self):
        assert strip_salutation("Frank Schwittay") == "Frank Schwittay"

    def test_none(self):
        assert strip_salutation(None) == ""


class TestNameForMatching:
    @pytest.mark.parametrize(
        "in_name,expected",
        [
            ("Herr Frank Schwittay", "frank schwittay"),
            ("Frau Dr. Maria Müller", "maria muller"),
            ("Herr Gérard Richter", "gerard richter"),
            ("Herr Timo Graf von Koenigsmarck", "timo graf von koenigsmarck"),
            ("Herr Dr. Rolf Eberwein", "rolf eberwein"),
        ],
    )
    def test_end_to_end(self, in_name, expected):
        assert name_for_matching(in_name) == expected
