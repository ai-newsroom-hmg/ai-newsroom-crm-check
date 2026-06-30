# Sonnet-Pipeline Runbook — Stand 2026-06-30

Frozen state of the OpenRouter-Sonnet-4.6 CRM-Aktualitätscheck-Pipeline,
nachdem 1842/1842 (100%) Zeilen der `Kopie von AD_D2600948_Mailing 2. Versand.xlsx`
erfolgreich verarbeitet wurden (⌀ konfidenz 0.90).

## Tag

`v0.1.0-sonnet-pipeline-2026-06-30`

## Architektur in einem Bild

```
Excel ──parser.py──> CrmContact[] ──cli.sonnet-check──┐
                                                      │ asyncio.Semaphore(20)
                                                      ▼
                                             sonnet_check.check_batch
                                                      │ batch=5
                                                      ▼
                              OpenRouter API  anthropic/claude-sonnet-4-6:online
                                                      │ extra_body.plugins=[{id:web,max_results:8}]
                                                      ▼
                                       SonnetVerdict[] (per row_idx)
                                                      │
                                                      ▼
                                excel_writer.write_sonnet_workbook
                                          │       │
                                          ▼       ▼
                                  Reiter 1     Reiter 2
                                  "Check"      "Anreicherung"
                              20 Original     Detail-View
                              + 8 Sonnet
```

## CLI

```bash
crm-check sonnet-check INPUT.xlsx \
  --out OUTPUT.xlsx \
  --verdicts-json AUDIT.jsonl \
  --batch-size 5 \
  --max-parallel 20 \
  --model anthropic/claude-sonnet-4-6:online \
  [--limit N]                    # erste N Zeilen
  [--random N --random-seed S]   # reproduzierbares Sample
  [--exclude-jsonl SKIP.jsonl]   # Doppel-Run-Schutz, mehrfach erlaubt
```

## Doppel-Run-Schutz

Jeder Run schreibt `--verdicts-json` mit einer JSON-Zeile pro `row_idx`.
Erfolgreiche row_idxs aus einem oder mehreren früheren Runs werden über
`--exclude-jsonl` als Skip-Liste übergeben und vor dem ersten API-Call
herausgefiltert (siehe `cli.py:sonnet_check_cmd`).

Verifikation der Disjunktheit (vor dem Re-Run der gerade gefahrenen 242):
```
All rows: 1842 / Skip (Erfolge): 1600 / Pending (offen): 242 / Overlap: 0
```

## Final-Merge

Mehrere JSONLs werden über `row_idx` dedupliziert; Success-Verdicts
überschreiben API-Fehler-Verdicts. Die FINAL-Excel hat exakt das gleiche
Layout wie ein einzelner Run, nur aus mehreren Pässen aggregiert.

## Run der am 30.06. lief

| Pass | Zeilen | Tool | Skip-Quelle | Output |
|---|---|---|---|---|
| 1. Random-50-Test | 50 | sonnet-check `--random 50 --random-seed 42` | — | `crm-check-random50-test-20260630-1508.{xlsx,jsonl}` |
| 2. Voll-Run | 1842 | sonnet-check | — | `crm-check-vollrun-20260630-1537.{xlsx,jsonl}` — 1195 Erfolg, 647 Limit-Hits |
| 3. Re-Run-647 | 647 | sonnet-check `--exclude-jsonl` Pass-2-Success | Pass-2-Success | `crm-check-rerun-647-20260630-1622.{xlsx,jsonl}` — 410 Erfolg, 237 Limit-Hits |
| 4. Re-Run-242 | 242 | sonnet-check `--exclude-jsonl` (1600 Skip) | Pass-1+2+3-Success | `crm-check-rerun-242-20260630-1636.{xlsx,jsonl}` — 242 Erfolg |
| 5. Final-Merge | 1842 | inline Python (siehe Memory) | — | `crm-check-FINAL-1842-20260630-1641.xlsx` |

## DSGVO

System-Prompt sendet pro Person nur `name`, `position`, `firma` an OpenRouter
→ Anthropic. **Keine Adresse, keine ID, keine Mail.** Der Audit-Trail
(JSONL + Excel-Reiter 2) bleibt lokal.

## Politischer Kontext im System-Prompt

Sonnet-4.6 hat per se den aktuellen Knowledge-Cutoff, aber der Prompt
verankert explizit:
- 21. Wahlperiode des Bundestags seit Februar 2025
- Kabinett Merz (CDU/SPD) seit Mai 2025: Wadephul (AA), Klingbeil (Finanzen),
  Bas (Arbeit), Reiche (Wirtschaft), Schnieder (Verkehr), Dobrindt (Inneres),
  Hubig (Justiz), Frei (Kanzleramt)
- Ampel-Minister (Heil, Lauterbach, Faeser, Habeck) **sind nicht mehr Minister**
- Nouripour = Vizepräsident Bundestag

Das war der Grund für den Pivot weg von Llama-3.3:70b (Knowledge-Cutoff
Q4-2024 → Kabinett Merz Feb 2025 unbekannt → falsche Verdikte).
