-- =============================================================================
-- 04_top_clientes_pareto.sql
-- Pregunta de negocio: ¿Se cumple la regla 80/20? ¿Que porcentaje de clientes
-- genera el 80% del gasto? Participacion acumulada de los clientes ordenados por gasto.
-- Requiere las vistas de 01_limpieza.sql.
-- =============================================================================

-- 4.1 Top 20 clientes con participacion acumulada
WITH gasto_cliente AS (
    SELECT cliente_id, SUM(monto_usd) AS gasto_usd, COUNT(*) AS transacciones
    FROM transacciones_aprobadas
    GROUP BY cliente_id
),
ranking AS (
    SELECT g.cliente_id, g.gasto_usd, g.transacciones,
           ROW_NUMBER() OVER (ORDER BY g.gasto_usd DESC) AS posicion,
           COUNT(*) OVER ()                               AS total_clientes,
           SUM(g.gasto_usd) OVER (ORDER BY g.gasto_usd DESC
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS gasto_acum,
           SUM(g.gasto_usd) OVER ()                       AS gasto_total
    FROM gasto_cliente g
)
SELECT r.posicion,
       c.nombre_empresa, c.pais, c.segmento,
       ROUND(r.gasto_usd, 0)                              AS gasto_usd,
       ROUND(100.0 * r.gasto_usd / r.gasto_total, 2)      AS participacion_pct,
       ROUND(100.0 * r.gasto_acum / r.gasto_total, 2)     AS participacion_acum_pct,
       ROUND(100.0 * r.posicion / r.total_clientes, 1)    AS pct_clientes_acum
FROM ranking r
JOIN clientes c ON c.cliente_id = r.cliente_id
ORDER BY r.posicion
LIMIT 20;

-- 4.2 Resumen Pareto: cuantos clientes concentran el 80% (y 50%) del gasto
WITH gasto_cliente AS (
    SELECT cliente_id, SUM(monto_usd) AS gasto_usd
    FROM transacciones_aprobadas
    GROUP BY cliente_id
),
ranking AS (
    SELECT cliente_id, gasto_usd,
           ROW_NUMBER() OVER (ORDER BY gasto_usd DESC) AS posicion,
           COUNT(*) OVER ()                             AS total_clientes,
           SUM(gasto_usd) OVER (ORDER BY gasto_usd DESC
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) * 1.0
               / SUM(gasto_usd) OVER ()                 AS acum
    FROM gasto_cliente
)
SELECT umbral_pct,
       MIN(posicion)                                    AS clientes_necesarios,
       MAX(total_clientes)                              AS total_clientes,
       ROUND(100.0 * MIN(posicion) / MAX(total_clientes), 1) AS pct_de_clientes
FROM (SELECT 50 AS umbral_pct UNION ALL SELECT 80 UNION ALL SELECT 90) u
JOIN ranking r ON r.acum >= u.umbral_pct / 100.0
GROUP BY umbral_pct
ORDER BY umbral_pct;

-- 4.3 Composicion del grupo que genera el 80% del gasto, por segmento
WITH gasto_cliente AS (
    SELECT cliente_id, SUM(monto_usd) AS gasto_usd
    FROM transacciones_aprobadas
    GROUP BY cliente_id
),
ranking AS (
    SELECT cliente_id, gasto_usd,
           SUM(gasto_usd) OVER (ORDER BY gasto_usd DESC
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) * 1.0
               / SUM(gasto_usd) OVER ()                 AS acum,
           SUM(gasto_usd) OVER (ORDER BY gasto_usd DESC
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) * 1.0
               / SUM(gasto_usd) OVER ()                 AS acum_previo
    FROM gasto_cliente
)
SELECT c.segmento,
       COUNT(*)                          AS clientes_en_top80,
       (SELECT COUNT(*) FROM clientes c2 WHERE c2.segmento = c.segmento) AS clientes_segmento,
       ROUND(SUM(r.gasto_usd), 0)        AS gasto_usd
FROM ranking r
JOIN clientes c ON c.cliente_id = r.cliente_id
WHERE COALESCE(r.acum_previo, 0) < 0.80      -- clientes que hacen falta para llegar al 80%
GROUP BY c.segmento
ORDER BY gasto_usd DESC;
