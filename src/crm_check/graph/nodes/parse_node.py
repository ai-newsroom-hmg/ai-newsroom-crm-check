"""Parse-Node — Excel-Row + CrmContact → CrmCheckState."""

from __future__ import annotations

from crm_check.graph.state import CrmCheckState
from crm_check.parser import CrmContact


def parse_row(contact: CrmContact) -> CrmCheckState:
    """Erzeugt initialen State aus einem geparsten CrmContact."""
    return CrmCheckState(
        row_idx=contact.row_idx,
        raw_row=contact.raw,
        salutation_name=contact.salutation_name or "",
        name_only=contact.name_only or "",
        position=contact.position,
        company=contact.company,
        street=contact.street,
        zip_city=contact.zip_city,
        country=contact.country,
        kg_candidates=[],
        kg_lobby_candidates=[],
        kg_entity_candidates=[],
        ni_candidates=[],
        pressrelations_hits=[],
        hugoplus_hits=[],
        openregister_candidates=[],
        social_profiles=[],
        errors=[],
        timings_ms={},
    )
