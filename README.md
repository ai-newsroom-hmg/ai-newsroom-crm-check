# ai-newsroom-crm-check

Agent-basierter CRM-Mailing-Listen-Check. Lädt eine Excel mit Personen (Name, Position, Firma, Adresse), prüft jede Zeile gegen die ai-newsroom-Entity-Intelligence (`kg.person_universe` + `ceq-api` + `news-intelligence` + hugoplus-Web-Search) und schreibt eine 2-Reiter-Excel zurück:

- **Reiter 1 (Original + Status):** Originalspalten + `aktuell` (true/false/null) + `bemerkung`
- **Reiter 2 (Anreicherung):** LinkedIn-URL, letzte Pressemention, Wechsel-Indikatoren, Konfidenz, Quellen

Spec: `/Users/gunternowy/.claude/plans/vivid-rolling-beacon.md`.

## Status

Phase 1a (Excel-Parser + Normalize + KG-Lookup + CLI). Service-Layer (FastAPI + LangGraph + Ollama) folgt in Phase 1b–d.

## Quickstart (Dev)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

CLI gegen Sample:

```bash
crm-check parse tests/fixtures/sample_10_synthetic.xlsx
```

## Konfiguration

Phase 1a:
- `KG_PG_DSN` — Postgres-DSN für `kg.person_universe` (lokales Docker-PG für Dev, `kg-postgres.knowledge-graph.svc.cluster.local` in GKE)

Spätere Phasen: `CEQ_API_URL`, `CEQ_API_TOKEN`, `NI_PG_DSN`, `OLLAMA_URL`, `HUGOPLUS_USER`, `HUGOPLUS_PASS`, `JWT_SECRET`.
