-- 002_estado_add_cancelado
-- Widen matches.estado CHECK to include 'cancelado'.
-- The scraper references 'cancelado' as a terminal state (TERMINAL_ESTADOS /
-- excluded_estados) but the original CHECK omitted it, so a write of that value
-- would have failed. Additive widening: no existing row uses a value outside the
-- new set (verified: estados in use = proximo, finalizado, esperando_resultado).
-- Safe to run more than once.

ALTER TABLE matches DROP CONSTRAINT IF EXISTS matches_estado_check;
ALTER TABLE matches ADD CONSTRAINT matches_estado_check
  CHECK (estado IN (
    'proximo', 'en_curso', 'finalizado', 'aplazado',
    'sin_resultado', 'esperando_resultado', 'cancelado'
  ));

-- Rollback:
--   ALTER TABLE matches DROP CONSTRAINT IF EXISTS matches_estado_check;
--   ALTER TABLE matches ADD CONSTRAINT matches_estado_check
--     CHECK (estado IN ('proximo','en_curso','finalizado','aplazado','sin_resultado','esperando_resultado'));
--   (only valid if no row has estado='cancelado')
