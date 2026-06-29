"""Normalize-Node — Anrede strippen + Vorname/Nachname trennen."""

from __future__ import annotations

from crm_check.graph.state import CrmCheckState
from crm_check.normalize import name_for_matching, strip_salutation


def normalize_name_node(state: CrmCheckState) -> CrmCheckState:
    raw = state.get("salutation_name") or state.get("name_only") or ""
    clean = strip_salutation(raw) or state.get("name_only", "")
    parts = [p for p in clean.split() if p]
    first = parts[0] if parts else ""
    last = parts[-1] if parts else ""
    return CrmCheckState(
        clean_name=clean,
        first_name=first,
        last_name=last,
        matching_key=name_for_matching(raw or clean),
    )
