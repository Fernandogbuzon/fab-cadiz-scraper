-- 005_logo_alias_fixes
-- Curated alias fixes BEYOND the logoMap.js port (004). These close coverage gaps
-- that exist in the web's current client-side logoMap too.
-- Idempotent. Add new fixes here as more name variants appear in matches.

DO $$
DECLARE
  v_club uuid;
BEGIN
  -- "CB CIUDAD DE CÁDIZ" appears in full in some competitions, but logoMap only had
  -- 'cbc cádiz'/'cbc cadiz' (the short form) -> the full name resolved to NO logo.
  SELECT id INTO v_club FROM clubs WHERE slug = 'cbciudaddecadiz';
  IF v_club IS NOT NULL THEN
    INSERT INTO club_team_aliases (club_id, team_name, is_primary, priority)
    VALUES (v_club, 'ciudad de cádiz', false, 43),
           (v_club, 'ciudad de cadiz', false, 42)
    ON CONFLICT (club_id, team_name) DO UPDATE SET priority = EXCLUDED.priority;
  END IF;
END $$;
