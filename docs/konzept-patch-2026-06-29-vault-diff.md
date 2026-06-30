# Konzept-Patch: CRM-Check vs Entity-Intelligence-Vault-Doku

**Erstellt:** 2026-06-29 nach Random-Sample-Test (7/10 = "?", LinkedIn-Discovery komplett fehlend)
**Trigger:** User-Feedback „LinkedIn-Suchen wurden gar nicht berücksichtigt. Alte Entity-Intelligence-Prozesse waren besser."

## Vault-Referenz-Architektur (8 Stufen, 98% Profil-Accuracy)

Quelle: `Konzepte/AI Newsroom/Entity Intelligence – Pipeline-Dokumentation.md` (Stand 19.04.2026)

| Stufe | Funktion | Status im CRM-Check |
|---|---|---|
| **B1 Pre-Filter** | Noise/Deceased (P570)/Fictional (P31)/Photographer (P106) | ✗ fehlt |
| **B2 Wikidata-First (SPARQL)** | P106/P39/P2002/P6634/P4003 etc. — 12 Plattformen | ⚠ wbsearchentities only, kein P-Property-Pull |
| **B3 NI-Kontext (ID-basiert)** | Mentions+IUs+Relations+Quotes, Tier Gold/Silber/Bronze | ⚠ name-basiert statt ID-basiert |
| **B4 SearXNG DE-IP** | ditschserver:18080, 70% Precision Few-Shot v4 | ✗ SearXNG nicht erreichbar |
| **B5 Ensemble-LLM Majority Voting** | 3× Qwen + 1× GPT-4.1-mini, ≥2/4 Übereinstimmung | ⚠ Single llama3.3:70b |
| **B6 7 Anti-Halluzinations-Gates** | Zitat + Citation-Verify + CoVe + Contrastive + Forbidden + HHEM + Wikidata-Cross-Check | ⚠ nur Match-Gate (1/7) |
| **B7 Social Profile Discovery** | 3-Stufen: Wikidata + SearXNG+LLM + Verifikation | ✗ KOMPLETT FEHLT |
| **B8 NI-Override** | Frische NI-Titel überschreiben veraltete Wikidata-Titel | ✗ fehlt |

## Was der Random-Sample-Test (10 Zeilen) gezeigt hat

- **Parser-Bug:** 3/10 Rows mit `name_only="Herr"` → Anrede nicht gestrippt
- **Wikidata-Claims:** 0/10 (B2 läuft nicht oder findet nichts)
- **LinkedIn-Claims:** 0/10 (B7 nicht implementiert)
- **WebSearch-Claims:** 0/10 (B4 SearXNG nicht erreichbar)
- **Hit-Rate:** 2/10 Identity-bestätigt (NI Lenhard, OpenRegister Mey), 5/10 nur Press-Headline ohne Person-Verifikation

## Priorisierter Implementation-Plan

| Priorität | Stufe | Aufwand | Erwarteter Boost |
|---|---|---|---|
| **P1** | Parser-Anrede-Strip (Herr/Frau/Dr./Prof./Graf von) | 30 min | +30% (3/10 Rows blind) |
| **P2** | B2 Wikidata-First mit SPARQL-Properties (P106/P39/P2002/P6634) | 1-2 h | +30-50% für Bundestag/Vorstände |
| **P3** | B7-Stufe-1 Social-Discovery (Wikidata-Social-Properties) | 1 h | LinkedIn-URLs für 30%+ |
| **P4** | B4 SearXNG-Tunnel von Air→ditschserver:18080 oder Air-IP | 30 min | Vorbedingung für P5 |
| **P5** | B7-Stufe-2 SearXNG+LLM Social-Discovery für QID-lose Personen | 2-3 h | LinkedIn für weitere 20% |
| **P6** | B6 zusätzliche Anti-Halluzinations-Gates (CoVe, HHEM) | 2-3 h | Konfidenz-Härtung |

**P1+P2+P3 zusammen ~3h** sind der Quick-Win mit ~70% erwarteter Hit-Rate-Steigerung.

## Lehre für Plan-Erstellung

Bei jeder zukünftigen Service-Architektur in Themen mit Vault-Doku **VOR Plan-Erstellung pflicht**:

```bash
mcp__mindloom__recherche "<service-thema> Pipeline Architektur"
```

Im CRM-Check-Plan hätte das die 4 Vault-Files (B1-B8 + Social Profile Pipeline) als Tier-2-Quellen geliefert. Stattdessen wurde der Plan **ohne diesen Sweep** geschrieben und kopierte die Stufen-Liste aus dem Memory-Footer statt aus der Master-Doku.

**Strukturelle Konsequenz:** ein Mindloom-Issue für Hard-Block-Mechanismus bei „neuer Plan ohne vorausgegangene Architektur-Recherche" — advisory-Hooks reichen nicht.
