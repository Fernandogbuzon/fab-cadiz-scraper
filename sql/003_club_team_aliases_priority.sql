-- 003_club_team_aliases_priority
-- Add a resolution-priority to club_team_aliases so DB-driven team→logo resolution
-- can reproduce the order-sensitivity of adesa80-web/src/lib/logoMap.js
-- (e.g. 'udea' must win over 'algeciras', 'gymnastica' over 'portuense').
-- Higher priority wins when several alias substrings match the same team name.
-- Additive + reversible. Safe to run more than once.

ALTER TABLE club_team_aliases
  ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_club_team_aliases_priority
  ON club_team_aliases(priority DESC);

-- Rollback:
--   DROP INDEX IF EXISTS idx_club_team_aliases_priority;
--   ALTER TABLE club_team_aliases DROP COLUMN IF EXISTS priority;
