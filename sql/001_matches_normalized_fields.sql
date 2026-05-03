-- Supabase Central migration.
-- Adds optional normalized fields used by the scraper and adesa80-web.
-- Safe to run more than once.

ALTER TABLE matches
  ADD COLUMN IF NOT EXISTS fecha_iso DATE,
  ADD COLUMN IF NOT EXISTS local_slug TEXT,
  ADD COLUMN IF NOT EXISTS visitante_slug TEXT,
  ADD COLUMN IF NOT EXISTS stable_match_key TEXT;

CREATE INDEX IF NOT EXISTS idx_matches_fecha_iso
  ON matches(fecha_iso);

CREATE INDEX IF NOT EXISTS idx_matches_local_slug
  ON matches(local_slug);

CREATE INDEX IF NOT EXISTS idx_matches_visitante_slug
  ON matches(visitante_slug);

CREATE INDEX IF NOT EXISTS idx_matches_stable_match_key
  ON matches(stable_match_key);

-- Backfill conservative values from existing legacy data.
-- local_slug/visitante_slug/stable_match_key should be overwritten by the scraper
-- on the next full run with the exact Python normalization contract.
UPDATE matches
SET fecha_iso = CASE
    WHEN fecha ~ '^\d{1,2}/\d{1,2}/\d{4}$'
      THEN to_date(fecha, 'DD/MM/YYYY')
    ELSE fecha_iso
  END
WHERE fecha_iso IS NULL;
