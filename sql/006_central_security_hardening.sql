-- 006_central_security_hardening
-- Resolves Supabase advisor findings on the Central project:
--   * 2x ERROR security_definer_view  (v_club_summary, v_scraper_history)
--   * function_search_path_mutable     (all 10 scraper RPCs)
--   * anon/authenticated_security_definer_function_executable (RPCs callable by anon)
--
-- Safe: scraper + adesa80-web both use the service_role key (unaffected). No consumer
-- calls these Central RPCs/views via the anon key (verified by grep over adesa80-web).
-- Reversible (see rollback notes at bottom).

-- 1) Views run as the querying user (RLS-respecting), not the definer.
CREATE OR REPLACE VIEW v_club_summary WITH (security_invoker = true) AS
SELECT
  c.id, c.name, c.slug, c.active,
  f.province AS federation,
  (SELECT count(*) FROM club_competitions cc WHERE cc.club_id = c.id AND cc.active = true) AS competitions_count,
  (SELECT count(*) FROM club_team_aliases cta WHERE cta.club_id = c.id) AS aliases_count,
  c.created_at
FROM clubs c
LEFT JOIN federations f ON f.id = c.federation_id;

CREATE OR REPLACE VIEW v_scraper_history WITH (security_invoker = true) AS
SELECT
  sr.id, sr.run_type, sr.status, sr.competitions_processed, sr.groups_scraped,
  sr.groups_skipped, sr.matches_upserted, sr.duration_seconds, sr.started_at,
  sr.finished_at, jsonb_array_length(sr.errors) AS error_count
FROM scraper_runs sr
ORDER BY sr.started_at DESC;

-- 2) Pin search_path (removes the mutable-search_path injection surface on DEFINER fns).
ALTER FUNCTION public.get_scraper_config()                                                              SET search_path = public;
ALTER FUNCTION public.get_unique_competition_urls()                                                     SET search_path = public;
ALTER FUNCTION public.check_content_changed(text, text, text)                                           SET search_path = public;
ALTER FUNCTION public.start_scraper_run(text, uuid[])                                                   SET search_path = public;
ALTER FUNCTION public.finish_scraper_run(uuid, text, integer, integer, integer, integer, jsonb)         SET search_path = public;
ALTER FUNCTION public.get_club_matches(text, text, text)                                                SET search_path = public;
ALTER FUNCTION public.get_pending_results(text)                                                         SET search_path = public;
ALTER FUNCTION public.link_club_competition(text, uuid)                                                 SET search_path = public;
ALTER FUNCTION public.cleanup_old_retries()                                                             SET search_path = public;
ALTER FUNCTION public.cleanup_old_hashes(integer)                                                       SET search_path = public;

-- 3) Restrict EXECUTE to service_role (scraper). Remove the default PUBLIC/anon/authenticated grant.
DO $$
DECLARE fn text;
BEGIN
  FOREACH fn IN ARRAY ARRAY[
    'public.get_scraper_config()',
    'public.get_unique_competition_urls()',
    'public.check_content_changed(text, text, text)',
    'public.start_scraper_run(text, uuid[])',
    'public.finish_scraper_run(uuid, text, integer, integer, integer, integer, jsonb)',
    'public.get_club_matches(text, text, text)',
    'public.get_pending_results(text)',
    'public.link_club_competition(text, uuid)',
    'public.cleanup_old_retries()',
    'public.cleanup_old_hashes(integer)'
  ] LOOP
    EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC, anon, authenticated', fn);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', fn);
  END LOOP;
END $$;

-- Rollback:
--   CREATE OR REPLACE VIEW ... (without WITH (security_invoker=true))  -- back to definer
--   ALTER FUNCTION ... RESET search_path;
--   GRANT EXECUTE ON FUNCTION ... TO PUBLIC;  -- per function
