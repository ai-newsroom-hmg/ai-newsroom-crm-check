-- Minimal-Seed für lokales kg-postgres (docker-compose).
-- Bildet das Subset des Production-Schemas ab, das CRM-Check liest.
-- Source-of-truth: hmg-knowledge-graph/init-db/{01-schema.sql, 10-person-universe.sql}
--
-- NICHT für Production. Nur synthetische Test-Personen. Echte Kundenliste
-- (sample_10.xlsx) ist via .gitignore ausgeschlossen.

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS kg;

-- ------------------------------------------------------------------
-- kg.entities (FK-Target für kg.person_universe.entity_id_canonical)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kg.entities (
    id              SERIAL PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    wikidata_id     TEXT,
    aliases         TEXT[] DEFAULT '{}',
    first_seen_at   TIMESTAMPTZ DEFAULT NOW(),
    total_mentions  INT DEFAULT 0,
    last_mentioned  TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}'
);

-- ------------------------------------------------------------------
-- kg.normalize_name — 1:1 aus 10-person-universe.sql:116
-- ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION kg.normalize_name(name TEXT)
RETURNS TEXT AS $$
SELECT lower(regexp_replace(unaccent(coalesce(name, '')), '\s+', ' ', 'g'));
$$ LANGUAGE SQL IMMUTABLE;

-- ------------------------------------------------------------------
-- kg.person_universe — Subset aus 10-person-universe.sql (Felder die CRM-Check liest)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kg.person_universe (
    person_id                BIGSERIAL PRIMARY KEY,
    wikidata_id              TEXT,
    entity_id_canonical      INT NOT NULL,
    full_name                TEXT NOT NULL,
    normalized_full_name     TEXT NOT NULL,
    name_aliases             TEXT[],
    first_seen               DATE NOT NULL,
    last_seen                DATE NOT NULL,
    is_active                BOOLEAN DEFAULT true,
    role                     TEXT,
    primary_org              TEXT,
    company_id               TEXT,
    linkedin_url             TEXT,
    linkedin_followers       INT,
    wikipedia_url            TEXT,
    twitter_url              TEXT,
    image_url                TEXT,
    sources                  JSONB DEFAULT '{}'::jsonb,
    -- In Production sind diese drei GENERATED ALWAYS AS (siehe 10-person-universe.sql).
    -- Lokal-PG16 lehnt NOW() in GENERATED-Expressions ab → manuell setzen.
    -- Lesendes Verhalten in CRM-Check ist identisch.
    is_stale_linkedin BOOLEAN DEFAULT false,
    is_stale_wikidata BOOLEAN DEFAULT false,
    is_stale_ceq BOOLEAN DEFAULT false,
    created_at               TIMESTAMPTZ DEFAULT NOW(),
    updated_at               TIMESTAMPTZ DEFAULT NOW(),
    FOREIGN KEY (entity_id_canonical) REFERENCES kg.entities(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_pu_normalized_trgm
    ON kg.person_universe USING gin (normalized_full_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_pu_last_seen ON kg.person_universe (last_seen DESC);

-- ------------------------------------------------------------------
-- Test-Seed — 6 synthetische Personen
-- ------------------------------------------------------------------
INSERT INTO kg.entities (canonical_name, entity_type) VALUES
    ('Anna Beispiel',     'PER'),
    ('Bernhard Test',     'PER'),
    ('Cordula Demo',      'PER'),
    ('Detlef Probe',      'PER'),
    ('Émilie Pröf',       'PER'),
    ('Friedrich Stale',   'PER');

INSERT INTO kg.person_universe (
    wikidata_id, entity_id_canonical, full_name, normalized_full_name,
    first_seen, last_seen, is_active, role, primary_org, company_id,
    linkedin_url, linkedin_followers, sources,
    is_stale_linkedin, is_stale_wikidata, is_stale_ceq
) VALUES
    ('Q1001', 1, 'Anna Beispiel',     kg.normalize_name('Anna Beispiel'),
     '2024-01-01', CURRENT_DATE, true, 'CEO', 'Beispiel AG', 'beispiel.ag',
     'https://linkedin.com/in/anna-beispiel', 1200,
     jsonb_build_object('linkedin_url', jsonb_build_object('ts', NOW(), 'ttl_days', 90)),
     false, false, false),

    ('Q1002', 2, 'Bernhard Test',     kg.normalize_name('Bernhard Test'),
     '2023-05-12', CURRENT_DATE - 30, true, 'Geschäftsführer', 'Test GmbH', 'test.gmbh',
     'https://linkedin.com/in/bernhard-test', 800,
     jsonb_build_object('linkedin_url', jsonb_build_object('ts', NOW() - interval '20 days', 'ttl_days', 90)),
     false, true, true),

    ('Q1003', 3, 'Dr. Cordula Demo', kg.normalize_name('Dr. Cordula Demo'),
     '2022-09-01', CURRENT_DATE - 5, true, 'Vorstand', 'Demo SE', 'demo.se',
     'https://linkedin.com/in/cordula-demo', 5400,
     jsonb_build_object('linkedin_url', jsonb_build_object('ts', NOW(), 'ttl_days', 90)),
     false, false, false),

    ('Q1004', 4, 'Detlef Probe',      kg.normalize_name('Detlef Probe'),
     '2021-01-15', CURRENT_DATE - 200, true, 'CFO', 'Probe AG', 'probe.ag',
     'https://linkedin.com/in/detlef-probe', 320,
     jsonb_build_object('linkedin_url', jsonb_build_object('ts', NOW() - interval '120 days', 'ttl_days', 90)),
     true, false, false),

    ('Q1005', 5, 'Émilie Pröf',       kg.normalize_name('Émilie Pröf'),
     '2025-03-10', CURRENT_DATE, true, 'COO', 'Pröf GmbH', 'proef.gmbh',
     NULL, NULL,
     jsonb_build_object('wikidata', jsonb_build_object('ts', NOW(), 'ttl_days', 30)),
     true, false, true),

    -- INACTIVE: ausgeschieden
    ('Q1006', 6, 'Friedrich Stale',   kg.normalize_name('Friedrich Stale'),
     '2019-06-01', '2024-04-01', false, 'ehemals CEO', 'Stale AG', 'stale.ag',
     'https://linkedin.com/in/friedrich-stale', 100,
     jsonb_build_object('linkedin_url', jsonb_build_object('ts', '2024-04-01T00:00:00Z', 'ttl_days', 90)),
     true, true, true);

ANALYZE kg.person_universe;
