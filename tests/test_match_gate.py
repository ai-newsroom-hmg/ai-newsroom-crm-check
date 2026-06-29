"""Hard-Negative-Suite gegen Surface-Match-False-Positives.

Quellen:
- Vault B6-Doku: „Patrick Schnieder" ≠ „Gordon Schnieder"
- Live-Vorfall 2026-06-29: Ulrike Pieper (Bahlsen) vs Ulrich Pieper (Lobby)
- Vault-Skill eigennamen-provenance-editor-pattern.md: Tobias/Sven Apel
"""

from __future__ import annotations

import pytest

from crm_check.graph.match_gate import passes_identity_gate


class TestHardNegatives:
    """Reject-Cases — Surface-Match darf NICHT durchgehen."""

    def test_pieper_gender_mismatch(self):
        """Ulrike (♀) vs Ulrich (♂) — Bahlsen vs Lobby — Live-Vorfall 2026-06-29."""
        d = passes_identity_gate(
            crm_first="Ulrike", crm_last="Pieper",
            crm_company="Bahlsen GmbH & Co. KG",
            src_first="Ulrich", src_last="Pieper",
            src_org=None,
        )
        assert d.accepted is False
        assert d.rule == "R1_gender_mismatch"

    def test_schnieder_firstname_mismatch(self):
        """B6-Doku: Patrick Schnieder ≠ Gordon Schnieder (beide männlich)."""
        d = passes_identity_gate(
            crm_first="Patrick", crm_last="Schnieder",
            crm_company="Beispiel AG",
            src_first="Gordon", src_last="Schnieder",
            src_org="Andere AG",
        )
        assert d.accepted is False
        # Beide männlich → R2 nicht R1
        assert d.rule == "R2_firstname_mismatch"

    def test_apel_firstname_mismatch(self):
        """Vault-Skill: Tobias Apel ≠ Sven Apel (LLM-Vornamen-Drift-Halluzination)."""
        d = passes_identity_gate(
            crm_first="Sven", crm_last="Apel",
            crm_company="Universität Magdeburg",
            src_first="Tobias", src_last="Apel",
            src_org=None,
        )
        assert d.accepted is False
        assert d.rule == "R2_firstname_mismatch"

    def test_different_last_name(self):
        d = passes_identity_gate(
            crm_first="Anna", crm_last="Schmidt",
            crm_company="X GmbH",
            src_first="Anna", src_last="Müller",
            src_org="X GmbH",
        )
        assert d.accepted is False
        assert d.rule == "R0_last_name_mismatch"

    def test_no_anchor_unknown_first_and_no_company_match(self):
        """Quelle ohne Vorname UND verschiedene Firmen → kein Anker."""
        d = passes_identity_gate(
            crm_first="Frank", crm_last="Schwittay",
            crm_company="Trend Micro Deutschland GmbH",
            src_first=None, src_last="Schwittay",
            src_org="Andere AG",
        )
        assert d.accepted is False
        assert d.rule == "R4_no_anchor"


class TestPositives:
    """Accept-Cases — echte Matches dürfen NICHT geblockt werden."""

    def test_exact_first_last_match(self):
        d = passes_identity_gate(
            crm_first="Frank", crm_last="Schwittay",
            crm_company="Trend Micro Deutschland GmbH",
            src_first="Frank", src_last="Schwittay",
            src_org="Trend Micro DE",
        )
        assert d.accepted is True
        assert d.rule == "R5_firstname_match"

    def test_sören_vs_soeren_alias(self):
        """Sören = Soeren = Soren — Variant der gleichen Person."""
        d = passes_identity_gate(
            crm_first="Sören", crm_last="Jautelat",
            crm_company="IBM",
            src_first="Soeren", src_last="Jautelat",
            src_org="IBM",
        )
        assert d.accepted is True
        assert d.rule == "R5_firstname_match"

    def test_company_anchor_when_source_first_unknown(self):
        """Pressemention erwähnt nur Nachname — Company-Match macht es eindeutig."""
        d = passes_identity_gate(
            crm_first="Frank", crm_last="Schwittay",
            crm_company="Trend Micro Deutschland GmbH",
            src_first=None, src_last="Schwittay",
            src_org="Trend Micro Deutschland",
        )
        assert d.accepted is True
        assert d.rule == "R3_company_anchor"

    def test_alex_alias(self):
        d = passes_identity_gate(
            crm_first="Alexander", crm_last="Müller",
            crm_company="SAP",
            src_first="Alex", src_last="Müller",
            src_org="SAP",
        )
        assert d.accepted is True
        assert d.rule == "R5_firstname_match"

    def test_umlaut_normalization_in_lastname(self):
        d = passes_identity_gate(
            crm_first="Klaus", crm_last="Müller",
            crm_company="X AG",
            src_first="Klaus", src_last="Mueller",
            src_org="X AG",
        )
        assert d.accepted is True
        assert d.rule == "R5_firstname_match"


class TestEdgeCases:
    def test_missing_last_name_rejects(self):
        d = passes_identity_gate(
            crm_first="Anna", crm_last="",
            crm_company="X",
            src_first="Anna", src_last="Schmidt",
            src_org="X",
        )
        assert d.accepted is False
        assert d.rule == "R0_missing_last_name"

    def test_gender_neutral_first_name_no_reject(self):
        """Kim (unisex) ↔ anderer Kim — Gender-Heuristik darf nicht reject feuern."""
        d = passes_identity_gate(
            crm_first="Kim", crm_last="Wagner",
            crm_company="X AG",
            src_first="Kim", src_last="Wagner",
            src_org="X AG",
        )
        assert d.accepted is True
