"""LangGraph StateGraph compile.

Topologie (drei Phasen — strukturelle Quellen, dann Web mit LLM-Verify):

    parse → normalize → fan_out_structured
                         ├── kg
                         ├── kg_lobby
                         ├── ni
                         ├── ceq
                         ├── openregister
                         └── wikidata
                                 ↓
                          websearch (conditional)
                                 ↓
                          verify (Llama-3.3:70b @ ruediger:11434)
                                 ↓
                              reason → END

`websearch` ist conditional: lookup_nodes.make_websearch_node skipt sich selbst
wenn eine strukturierte Quelle bereits einen plausiblen Hit hatte.

`verify` ist Pipeline-v2 "VERIFY+QA"-Stufe: WebSearch-Snippets zaehlen nur dann
als Person-Evidence, wenn Llama-3.3:70b ihren Bezug zum CRM-Eintrag bestaetigt.
Ohne OLLAMA_BASE_URL → person_confirmed=False, Hits werden verworfen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import asyncpg

from crm_check.graph.nodes.lookup_nodes import (
    make_ceq_node,
    make_kg_lobby_node,
    make_kg_node,
    make_ni_node,
    make_openregister_node,
    make_websearch_node,
    make_wikidata_node,
)
from crm_check.graph.nodes.correlate_node import make_correlate_node
from crm_check.graph.nodes.normalize_node import normalize_name_node
from crm_check.graph.nodes.reason_node import llm_reason, rule_based_reason
from crm_check.graph.nodes.verify_node import make_verify_node
from crm_check.graph.state import CrmCheckState


@dataclass
class GraphDeps:
    """Connections + Clients die der Graph braucht."""

    kg_pool: asyncpg.Pool | None = None
    ni_pool: asyncpg.Pool | None = None
    ceq_client: Any | None = None
    use_llm_reason: bool = False
    use_websearch: bool = True
    use_wikidata: bool = True
    use_openregister: bool = True

    @classmethod
    async def open(
        cls,
        *,
        kg_dsn: str | None = None,
        ni_dsn: str | None = None,
        ceq_url: str | None = None,
        ceq_token: str | None = None,
        use_llm_reason: bool | None = None,
        use_websearch: bool = True,
        use_wikidata: bool = True,
        use_openregister: bool = True,
    ) -> "GraphDeps":
        deps = cls(
            use_websearch=use_websearch,
            use_wikidata=use_wikidata,
            use_openregister=use_openregister,
        )
        if kg_dsn:
            deps.kg_pool = await asyncpg.create_pool(
                kg_dsn, min_size=1, max_size=4, command_timeout=10
            )
        if ni_dsn:
            deps.ni_pool = await asyncpg.create_pool(
                ni_dsn, min_size=1, max_size=4, command_timeout=10
            )
        if ceq_url and ceq_token:
            from crm_check.graph.nodes.ceq_lookup import CeqClient
            deps.ceq_client = CeqClient(ceq_url, ceq_token)
            await deps.ceq_client.__aenter__()
        if use_llm_reason is not None:
            deps.use_llm_reason = use_llm_reason
        else:
            deps.use_llm_reason = bool(os.getenv("OLLAMA_BASE_URL"))
        return deps

    async def close(self) -> None:
        if self.ceq_client:
            try:
                await self.ceq_client.__aexit__(None, None, None)
            except Exception:
                pass
        if self.ni_pool:
            await self.ni_pool.close()
        if self.kg_pool:
            await self.kg_pool.close()


def build_graph(deps: GraphDeps):
    """Zwei-Phasen-Pipeline: strukturierte Quellen → WebSearch-Fallback → Reason."""
    from langgraph.graph import END, StateGraph

    g = StateGraph(CrmCheckState)

    g.add_node("normalize", normalize_name_node)
    g.add_node("kg", make_kg_node(deps.kg_pool))
    g.add_node("kg_lobby", make_kg_lobby_node(deps.kg_pool))
    g.add_node("ni", make_ni_node(deps.ni_pool))
    g.add_node("ceq", make_ceq_node(deps.ceq_client))
    g.add_node("openregister", make_openregister_node())
    g.add_node("wikidata", make_wikidata_node())
    g.add_node("websearch", make_websearch_node(enabled=deps.use_websearch))
    g.add_node("verify", make_verify_node())
    g.add_node("correlate", make_correlate_node())
    g.add_node("reason", llm_reason if deps.use_llm_reason else rule_based_reason)

    g.set_entry_point("normalize")
    structured = ["kg", "kg_lobby", "ni", "ceq", "openregister", "wikidata"]
    for n in structured:
        g.add_edge("normalize", n)
        # Strukturierte → websearch (fan-in damit websearch alle gesehen hat)
        g.add_edge(n, "websearch")
    # Pipeline-v2 VERIFY+QA-Stufe: WebSearch-Snippets erst durch Llama pruefen
    g.add_edge("websearch", "verify")
    # Pipeline-v2 CORRELATE+MERGE-Stufe: Claims aus allen Quellen konsolidieren
    g.add_edge("verify", "correlate")
    g.add_edge("correlate", "reason")
    g.add_edge("reason", END)

    return g.compile()
