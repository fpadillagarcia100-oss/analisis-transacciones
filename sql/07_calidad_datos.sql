-- =============================================================================
-- 07_calidad_datos.sql
-- Pregunta de negocio: ¿Que tan confiable es la tabla de transacciones y que
-- problemas hay que corregir en origen? Conteo de cada tipo de problema sobre
-- la tabla CRUDA (no la vista limpia), para el reporte de calidad de datos.
-- =============================================================================

WITH numeradas AS (
    SELECT t.*,
           ROW_NUMBER() OVER (PARTITION BY tarjeta_id, fecha_hora, monto, comercio
                              ORDER BY transaccion_id) AS rn
    FROM transacciones t
),
total AS (SELECT COUNT(*) AS n FROM transacciones),
problemas AS (
    SELECT 'Duplicados (misma tarjeta, fecha_hora, monto, comercio)' AS problema,
           COUNT(*) AS filas
    FROM numeradas WHERE rn > 1 AND comercio IS NOT NULL
    UNION ALL
    SELECT 'comercio NULL', COUNT(*) FROM transacciones WHERE comercio IS NULL
    UNION ALL
    SELECT 'Monto negativo que no es reversa', COUNT(*)
    FROM transacciones WHERE monto < 0 AND estatus <> 'reversada'
    UNION ALL
    SELECT 'fecha_hora en el futuro (> 2026-09-30 23:59:59)', COUNT(*)
    FROM transacciones WHERE fecha_hora > '2026-09-30 23:59:59'
    UNION ALL
    SELECT 'Reversadas con monto positivo', COUNT(*)
    FROM transacciones WHERE estatus = 'reversada' AND monto > 0
    UNION ALL
    SELECT 'Monto NULL o cero', COUNT(*) FROM transacciones WHERE monto IS NULL OR monto = 0
    UNION ALL
    SELECT 'Transaccion con cliente distinto al dueno de la tarjeta', COUNT(*)
    FROM transacciones t JOIN tarjetas j ON j.tarjeta_id = t.tarjeta_id
    WHERE t.cliente_id <> j.cliente_id
    UNION ALL
    SELECT 'Transaccion anterior a la emision de la tarjeta', COUNT(*)
    FROM transacciones t JOIN tarjetas j ON j.tarjeta_id = t.tarjeta_id
    WHERE substr(t.fecha_hora, 1, 10) < j.fecha_emision
    UNION ALL
    SELECT 'Transaccion anterior al alta del cliente', COUNT(*)
    FROM transacciones t JOIN clientes c ON c.cliente_id = t.cliente_id
    WHERE substr(t.fecha_hora, 1, 10) < c.fecha_alta
    UNION ALL
    SELECT 'Tarjetas sin ninguna transaccion', COUNT(*)
    FROM tarjetas j WHERE NOT EXISTS (SELECT 1 FROM transacciones t WHERE t.tarjeta_id = j.tarjeta_id)
)
SELECT problema,
       filas,
       ROUND(100.0 * filas / (SELECT n FROM total), 3) AS pct_del_total
FROM problemas
ORDER BY filas DESC;

-- Resumen: filas totales vs filas utilizables
SELECT (SELECT COUNT(*) FROM transacciones)         AS filas_crudas,
       (SELECT COUNT(*) FROM transacciones_limpias) AS filas_limpias,
       (SELECT COUNT(*) FROM transacciones) - (SELECT COUNT(*) FROM transacciones_limpias) AS filas_descartadas,
       ROUND(100.0 * (SELECT COUNT(*) FROM transacciones_limpias) / (SELECT COUNT(*) FROM transacciones), 2) AS pct_utilizable;
