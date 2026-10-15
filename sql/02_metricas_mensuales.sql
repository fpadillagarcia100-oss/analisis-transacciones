-- =============================================================================
-- 02_metricas_mensuales.sql
-- Pregunta de negocio: ¿Como evoluciona el negocio mes a mes? Volumen procesado
-- (USD, solo aprobadas), numero de transacciones, ticket promedio, tasa de
-- aprobacion y crecimiento mensual (MoM).
-- Requiere las vistas de 01_limpieza.sql.
-- =============================================================================

WITH mensual AS (
    SELECT mes,
           COUNT(*)                                                    AS transacciones,
           SUM(CASE WHEN estatus = 'aprobada'  THEN 1 ELSE 0 END)      AS aprobadas,
           SUM(CASE WHEN estatus = 'rechazada' THEN 1 ELSE 0 END)      AS rechazadas,
           SUM(CASE WHEN estatus = 'reversada' THEN 1 ELSE 0 END)      AS reversadas,
           SUM(CASE WHEN estatus = 'aprobada'  THEN monto_usd END)     AS volumen_usd,
           AVG(CASE WHEN estatus = 'aprobada'  THEN monto_usd END)     AS ticket_prom_usd
    FROM transacciones_limpias
    GROUP BY mes
)
SELECT mes,
       transacciones,
       aprobadas,
       ROUND(volumen_usd, 0)                                           AS volumen_usd,
       ROUND(ticket_prom_usd, 2)                                       AS ticket_prom_usd,
       ROUND(100.0 * aprobadas  / transacciones, 2)                    AS tasa_aprobacion_pct,
       ROUND(100.0 * rechazadas / transacciones, 2)                    AS tasa_rechazo_pct,
       ROUND(100.0 * (volumen_usd - LAG(volumen_usd) OVER (ORDER BY mes))
             / LAG(volumen_usd) OVER (ORDER BY mes), 2)                AS crecimiento_mom_vol_pct,
       ROUND(100.0 * (transacciones - LAG(transacciones) OVER (ORDER BY mes))
             / LAG(transacciones) OVER (ORDER BY mes), 2)              AS crecimiento_mom_tx_pct
FROM mensual
ORDER BY mes;
