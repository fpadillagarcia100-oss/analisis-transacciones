-- =============================================================================
-- 06_anomalias.sql
-- Pregunta de negocio: ¿Que tarjetas muestran comportamiento sospechoso?
--   6.1 Montos que superan 3 desviaciones estandar sobre la media de la propia tarjeta
--       (sobre transacciones aprobadas, tarjetas con >= 30 transacciones).
--   6.2 Tarjetas candidatas a fraude por picos: >= 3 transacciones >= 25x la mediana de la tarjeta
--       (criterio robusto: la media y la desviacion se contaminan con los propios picos).
--   6.3 Card testing: >= 5 transacciones menores a USD 5 en una ventana de 10 minutos
--       en la misma tarjeta (cualquier estatus).
-- SQLite no tiene STDEV: se usa SQRT(AVG(x*x) - AVG(x)*AVG(x)).
-- Requiere las vistas de 01_limpieza.sql.
-- =============================================================================

-- 6.1 Transacciones con z-score > 3 respecto a su propia tarjeta (top 30 por z)
WITH stats AS (
    SELECT tarjeta_id,
           COUNT(*)                                                       AS n,
           AVG(monto_usd)                                                 AS media,
           SQRT(MAX(AVG(monto_usd * monto_usd) - AVG(monto_usd) * AVG(monto_usd), 0)) AS desv
    FROM transacciones_aprobadas
    GROUP BY tarjeta_id
    HAVING COUNT(*) >= 30
)
SELECT t.transaccion_id, t.tarjeta_id, t.fecha_hora, t.categoria_comercio, t.comercio,
       ROUND(t.monto_usd, 2)                  AS monto_usd,
       ROUND(s.media, 2)                      AS media_tarjeta_usd,
       ROUND(s.desv, 2)                       AS desv_tarjeta_usd,
       ROUND((t.monto_usd - s.media) / s.desv, 2) AS z_score,
       ROUND(t.monto_usd / s.media, 1)        AS veces_la_media
FROM transacciones_aprobadas t
JOIN stats s ON s.tarjeta_id = t.tarjeta_id
WHERE s.desv > 0 AND (t.monto_usd - s.media) / s.desv > 3
ORDER BY z_score DESC
LIMIT 30;

-- 6.2 Tarjetas con picos probables de fraude (criterio robusto): >= 3 transacciones aprobadas
--     por >= 25x la MEDIANA de la propia tarjeta. La media/desviacion se contaminan con los
--     propios picos (efecto de enmascaramiento) y la cola log-normal da falsos positivos con z>3;
--     la mediana es resistente a ambos.
WITH ordenadas AS (
    SELECT tarjeta_id, monto_usd,
           ROW_NUMBER() OVER (PARTITION BY tarjeta_id ORDER BY monto_usd) AS rn,
           COUNT(*)     OVER (PARTITION BY tarjeta_id)                   AS n
    FROM transacciones_aprobadas
),
medianas AS (
    SELECT tarjeta_id, MAX(n) AS n, AVG(monto_usd) AS mediana
    FROM ordenadas
    WHERE rn IN ((n + 1) / 2, (n + 2) / 2)
    GROUP BY tarjeta_id
    HAVING MAX(n) >= 30
),
marcadas AS (
    SELECT t.tarjeta_id, t.monto_usd, t.monto_usd / m.mediana AS veces_mediana, m.mediana, m.n
    FROM transacciones_aprobadas t
    JOIN medianas m ON m.tarjeta_id = t.tarjeta_id
    WHERE t.monto_usd >= 25 * m.mediana
)
SELECT tarjeta_id,
       n                                  AS tx_tarjeta,
       COUNT(*)                           AS tx_marcadas,
       ROUND(MAX(veces_mediana), 1)       AS max_veces_mediana,
       ROUND(mediana, 2)                  AS mediana_usd,
       ROUND(SUM(monto_usd), 0)           AS monto_marcado_usd
FROM marcadas
GROUP BY tarjeta_id
HAVING COUNT(*) >= 3
ORDER BY monto_marcado_usd DESC;

-- 6.3 Card testing: rafagas de >= 5 microtransacciones (< USD 5) en 10 minutos
WITH pequenas AS (
    SELECT transaccion_id, tarjeta_id, fecha_hora, monto_usd, estatus,
           julianday(fecha_hora) AS jd
    FROM transacciones_limpias
    WHERE monto_usd > 0 AND monto_usd < 5
),
ventanas AS (   -- para cada transaccion pequena: cuantas hay en los siguientes 10 minutos
    SELECT a.tarjeta_id, a.fecha_hora AS inicio, a.transaccion_id,
           COUNT(*) AS intentos_10min,
           SUM(CASE WHEN b.estatus = 'rechazada' THEN 1 ELSE 0 END) AS rechazados,
           MAX(b.fecha_hora) AS fin
    FROM pequenas a
    JOIN pequenas b
      ON b.tarjeta_id = a.tarjeta_id
     AND b.jd >= a.jd
     AND b.jd <= a.jd + 10.0 / 1440.0
    GROUP BY a.tarjeta_id, a.transaccion_id, a.fecha_hora
    HAVING COUNT(*) >= 5
)
SELECT tarjeta_id,
       MIN(inicio)             AS primera_rafaga,
       MAX(intentos_10min)     AS max_intentos_en_10min,
       MAX(rechazados)         AS rechazados_en_ventana,
       COUNT(DISTINCT date(inicio)) AS dias_con_rafaga
FROM ventanas
GROUP BY tarjeta_id
ORDER BY max_intentos_en_10min DESC, tarjeta_id;
