-- =============================================================================
-- 05_cohortes_retencion.sql
-- Pregunta de negocio: De los clientes que empiezan a usar sus tarjetas cada mes,
-- ¿que porcentaje sigue transaccionando (aprobadas) 1, 2, 3... meses despues?
--
-- Definicion (corregida en fase 2):
--   * Cohorte = mes de la PRIMERA transaccion aprobada del cliente.
--   * Solo clientes dados de alta dentro de la ventana (fecha_alta >= 2025-10-01):
--     para los clientes antiguos el verdadero primer mes no es observable.
--   * Retencion en el mes k = % de la cohorte con >= 1 compra aprobada en el mes k.
--   * Las celdas fuera de la ventana observable (despues de 2026-09) quedan NULL.
--
-- Por que se cambio: la version anterior usaba cohorte = mes de fecha_alta. Las
-- tarjetas se emiten dias o semanas despues del alta, asi que muchos clientes aun
-- no habian comprado en su mes de alta (m0 = 37%). Los primeros meses median la
-- ACTIVACION y no la retencion, y la curva "subia" con el tiempo (77.8% en m1,
-- 98.2% en m3), algo imposible para una retencion bien definida. La consulta 5.3
-- cuantifica ese rezago de activacion.
-- Requiere las vistas de 01_limpieza.sql. Mismos numeros que python/analisis.py.
-- =============================================================================

-- 5.1 Matriz de retencion por cohorte (% de la cohorte activa en el mes k)
WITH actividad AS (   -- un registro por cliente nuevo y mes con al menos una compra aprobada
    SELECT DISTINCT a.cliente_id, a.mes,
           CAST(substr(a.mes, 1, 4) AS INTEGER) * 12 + CAST(substr(a.mes, 6, 2) AS INTEGER) AS idx_mes
    FROM transacciones_aprobadas a
    JOIN clientes c ON c.cliente_id = a.cliente_id
    WHERE c.fecha_alta >= '2025-10-01'
),
cohortes AS (         -- cohorte = mes de la primera compra aprobada
    SELECT cliente_id, MIN(mes) AS cohorte, MIN(idx_mes) AS idx_cohorte
    FROM actividad
    GROUP BY cliente_id
),
tamano AS (
    SELECT cohorte, idx_cohorte, COUNT(*) AS clientes
    FROM cohortes
    GROUP BY cohorte, idx_cohorte
),
offsets(n) AS (
    SELECT 0 UNION ALL SELECT n + 1 FROM offsets WHERE n < 11
),
activos AS (
    SELECT c.cohorte, a.idx_mes - c.idx_cohorte AS mes_n, COUNT(*) AS activos
    FROM cohortes c
    JOIN actividad a ON a.cliente_id = c.cliente_id
    GROUP BY c.cohorte, a.idx_mes - c.idx_cohorte
),
-- ultimo mes observable en los datos = 2026-09
rejilla AS (
    SELECT t.cohorte, t.clientes, o.n AS mes_n,
           COALESCE(ac.activos, 0) AS activos
    FROM tamano t
    CROSS JOIN offsets o
    LEFT JOIN activos ac ON ac.cohorte = t.cohorte AND ac.mes_n = o.n
    WHERE t.idx_cohorte + o.n <= 2026 * 12 + 9
)
SELECT cohorte,
       clientes,
       MAX(CASE WHEN mes_n = 0  THEN ROUND(100.0 * activos / clientes, 1) END) AS m0,
       MAX(CASE WHEN mes_n = 1  THEN ROUND(100.0 * activos / clientes, 1) END) AS m1,
       MAX(CASE WHEN mes_n = 2  THEN ROUND(100.0 * activos / clientes, 1) END) AS m2,
       MAX(CASE WHEN mes_n = 3  THEN ROUND(100.0 * activos / clientes, 1) END) AS m3,
       MAX(CASE WHEN mes_n = 4  THEN ROUND(100.0 * activos / clientes, 1) END) AS m4,
       MAX(CASE WHEN mes_n = 5  THEN ROUND(100.0 * activos / clientes, 1) END) AS m5,
       MAX(CASE WHEN mes_n = 6  THEN ROUND(100.0 * activos / clientes, 1) END) AS m6,
       MAX(CASE WHEN mes_n = 9  THEN ROUND(100.0 * activos / clientes, 1) END) AS m9
FROM rejilla
GROUP BY cohorte, clientes
ORDER BY cohorte;

-- 5.2 Curva de retencion ponderada (todas las cohortes). Una cohorte solo aporta al
--     mes k si ese mes es observable; con menos de ~30 clientes elegibles el punto es ruidoso.
WITH actividad AS (
    SELECT DISTINCT a.cliente_id,
           CAST(substr(a.mes, 1, 4) AS INTEGER) * 12 + CAST(substr(a.mes, 6, 2) AS INTEGER) AS idx_mes
    FROM transacciones_aprobadas a
    JOIN clientes c ON c.cliente_id = a.cliente_id
    WHERE c.fecha_alta >= '2025-10-01'
),
cohortes AS (
    SELECT cliente_id, MIN(idx_mes) AS idx_cohorte FROM actividad GROUP BY cliente_id
),
offsets(n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM offsets WHERE n < 11),
elegibles AS (   -- clientes cuyo mes n ya es observable
    SELECT o.n AS mes_n, COUNT(*) AS clientes
    FROM cohortes c CROSS JOIN offsets o
    WHERE c.idx_cohorte + o.n <= 2026 * 12 + 9
    GROUP BY o.n
),
retenidos AS (
    SELECT a.idx_mes - c.idx_cohorte AS mes_n, COUNT(*) AS activos
    FROM cohortes c JOIN actividad a ON a.cliente_id = c.cliente_id
    GROUP BY a.idx_mes - c.idx_cohorte
)
SELECT e.mes_n, e.clientes AS clientes_elegibles, COALESCE(r.activos, 0) AS activos,
       ROUND(100.0 * COALESCE(r.activos, 0) / e.clientes, 1) AS retencion_pct
FROM elegibles e LEFT JOIN retenidos r ON r.mes_n = e.mes_n
WHERE e.mes_n <= 10
ORDER BY e.mes_n;

-- 5.3 Rezago de activacion (por que la cohorte NO debe ser el mes de alta):
--     dias entre el alta, la primera tarjeta emitida y la primera compra aprobada.
WITH nuevos AS (
    SELECT cliente_id, fecha_alta FROM clientes WHERE fecha_alta >= '2025-10-01'
),
primera_compra AS (
    SELECT cliente_id, MIN(fecha_hora) AS primera FROM transacciones_aprobadas GROUP BY cliente_id
),
primera_tarjeta AS (
    SELECT cliente_id, MIN(fecha_emision) AS emision FROM tarjetas GROUP BY cliente_id
)
SELECT COUNT(*)                                                              AS clientes_nuevos,
       SUM(CASE WHEN p.primera IS NULL THEN 1 ELSE 0 END)                    AS sin_compras,
       ROUND(AVG(julianday(t.emision) - julianday(n.fecha_alta)), 1)         AS dias_prom_alta_a_tarjeta,
       ROUND(AVG(julianday(date(p.primera)) - julianday(n.fecha_alta)), 1)   AS dias_prom_alta_a_compra,
       ROUND(100.0 * SUM(CASE WHEN substr(p.primera, 1, 7) = substr(n.fecha_alta, 1, 7) THEN 1 ELSE 0 END)
             / COUNT(p.primera), 1)                                          AS pct_compra_en_mes_de_alta
FROM nuevos n
LEFT JOIN primera_compra  p ON p.cliente_id = n.cliente_id
LEFT JOIN primera_tarjeta t ON t.cliente_id = n.cliente_id;
