-- =============================================================================
-- 03_segmentos_y_categorias.sql
-- Pregunta de negocio: ¿Donde se concentra el gasto? Gasto aprobado (USD) por
-- segmento, pais del cliente y categoria de comercio, con su participacion.
-- Requiere las vistas de 01_limpieza.sql.
-- =============================================================================

-- 3.1 Por segmento
SELECT c.segmento,
       COUNT(DISTINCT c.cliente_id)                                   AS clientes_activos,
       COUNT(*)                                                       AS transacciones,
       ROUND(SUM(t.monto_usd), 0)                                     AS gasto_usd,
       ROUND(AVG(t.monto_usd), 2)                                     AS ticket_prom_usd,
       ROUND(100.0 * SUM(t.monto_usd) / SUM(SUM(t.monto_usd)) OVER (), 2) AS participacion_pct,
       ROUND(SUM(t.monto_usd) / COUNT(DISTINCT c.cliente_id), 0)      AS gasto_por_cliente_usd
FROM transacciones_aprobadas t
JOIN clientes c ON c.cliente_id = t.cliente_id
GROUP BY c.segmento
ORDER BY gasto_usd DESC;

-- 3.2 Por pais del cliente
SELECT c.pais,
       COUNT(DISTINCT c.cliente_id)                                   AS clientes_activos,
       COUNT(*)                                                       AS transacciones,
       ROUND(SUM(t.monto_usd), 0)                                     AS gasto_usd,
       ROUND(AVG(t.monto_usd), 2)                                     AS ticket_prom_usd,
       ROUND(100.0 * SUM(t.monto_usd) / SUM(SUM(t.monto_usd)) OVER (), 2) AS participacion_pct
FROM transacciones_aprobadas t
JOIN clientes c ON c.cliente_id = t.cliente_id
GROUP BY c.pais
ORDER BY gasto_usd DESC;

-- 3.3 Por categoria (con ranking y acumulado)
WITH por_cat AS (
    SELECT categoria_comercio,
           COUNT(*)          AS transacciones,
           SUM(monto_usd)    AS gasto_usd,
           AVG(monto_usd)    AS ticket_prom_usd
    FROM transacciones_aprobadas
    GROUP BY categoria_comercio
)
SELECT categoria_comercio,
       transacciones,
       ROUND(gasto_usd, 0)                                   AS gasto_usd,
       ROUND(ticket_prom_usd, 2)                             AS ticket_prom_usd,
       ROUND(100.0 * gasto_usd / SUM(gasto_usd) OVER (), 2)  AS participacion_pct,
       ROUND(100.0 * SUM(gasto_usd) OVER (ORDER BY gasto_usd DESC
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
             / SUM(gasto_usd) OVER (), 2)                    AS participacion_acum_pct
FROM por_cat
ORDER BY gasto_usd DESC;

-- 3.4 Mezcla de categorias dentro de cada segmento (participacion por segmento)
SELECT c.segmento,
       t.categoria_comercio,
       ROUND(SUM(t.monto_usd), 0) AS gasto_usd,
       ROUND(100.0 * SUM(t.monto_usd) / SUM(SUM(t.monto_usd)) OVER (PARTITION BY c.segmento), 2)
             AS pct_dentro_segmento
FROM transacciones_aprobadas t
JOIN clientes c ON c.cliente_id = t.cliente_id
GROUP BY c.segmento, t.categoria_comercio
ORDER BY c.segmento, gasto_usd DESC;

-- 3.5 Pais x segmento (matriz de gasto)
SELECT c.pais, c.segmento,
       ROUND(SUM(t.monto_usd), 0) AS gasto_usd,
       ROUND(100.0 * SUM(t.monto_usd) / SUM(SUM(t.monto_usd)) OVER (), 2) AS participacion_pct
FROM transacciones_aprobadas t
JOIN clientes c ON c.cliente_id = t.cliente_id
GROUP BY c.pais, c.segmento
ORDER BY gasto_usd DESC;
