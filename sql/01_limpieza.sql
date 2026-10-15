-- =============================================================================
-- 01_limpieza.sql
-- Pregunta de negocio: ¿Que transacciones son confiables para analizar?
-- Crea las vistas base que usan todos los demas scripts:
--   * tasas_cambio          : tasa fija por USD para normalizar montos multi-moneda
--   * transacciones_limpias : sin duplicados, sin comercio NULL, sin negativos que
--                             no sean reversas, sin fechas futuras; con monto_usd y mes
--   * transacciones_aprobadas: subconjunto aprobado (base del gasto/volumen)
-- Reglas: duplicado = misma (tarjeta_id, fecha_hora, monto, comercio); se conserva
-- el transaccion_id mas bajo. Corte de "futuro" = 2026-09-30 23:59:59.
-- =============================================================================

DROP VIEW IF EXISTS transacciones_aprobadas;
DROP VIEW IF EXISTS transacciones_limpias;
DROP VIEW IF EXISTS tasas_cambio;

CREATE VIEW tasas_cambio AS
SELECT 'MXN' AS moneda, 18.5   AS tasa_por_usd UNION ALL
SELECT 'COP',           4100.0                 UNION ALL
SELECT 'BRL',           5.4                    UNION ALL
SELECT 'USD',           1.0;

CREATE VIEW transacciones_limpias AS
WITH numeradas AS (
    SELECT t.*,
           ROW_NUMBER() OVER (
               PARTITION BY tarjeta_id, fecha_hora, monto, comercio
               ORDER BY transaccion_id
           ) AS rn
    FROM transacciones t
)
SELECT n.transaccion_id,
       n.tarjeta_id,
       n.cliente_id,
       n.fecha_hora,
       substr(n.fecha_hora, 1, 7)  AS mes,
       n.monto,
       n.moneda,
       n.monto / tc.tasa_por_usd   AS monto_usd,
       n.categoria_comercio,
       n.comercio,
       n.pais_comercio,
       n.estatus
FROM numeradas n
JOIN tasas_cambio tc ON tc.moneda = n.moneda
WHERE n.rn = 1                                            -- sin duplicados
  AND n.comercio IS NOT NULL                              -- comercio informado
  AND NOT (n.monto < 0 AND n.estatus <> 'reversada')      -- negativos solo si son reversas
  AND n.fecha_hora <= '2026-09-30 23:59:59';              -- sin fechas futuras

CREATE VIEW transacciones_aprobadas AS
SELECT * FROM transacciones_limpias WHERE estatus = 'aprobada';

-- Verificacion: filas crudas vs limpias
WITH crudas AS (SELECT COUNT(*) AS n FROM transacciones),
     limpias AS (SELECT COUNT(*) AS n FROM transacciones_limpias)
SELECT crudas.n AS filas_crudas,
       limpias.n AS filas_limpias,
       crudas.n - limpias.n AS filas_removidas,
       ROUND(100.0 * (crudas.n - limpias.n) / crudas.n, 2) AS pct_removido
FROM crudas, limpias;
