# Problemas y anomalias inyectados (dataset sintetico, seed 42)

Dataset generado con `python/generar_datos.py` (numpy seed 42). Total de filas en `transacciones`: **120,267**
(300 clientes, 900 tarjetas). Ventana: 2025-10-01 a 2026-09-30.
El archivo `data/anomalias_inyectadas.csv` (ignorado por git) contiene el ground truth fila por fila.

## Convenciones del dataset
- Las transacciones **reversadas** tienen `monto` NEGATIVO (contrapartida del cargo). Un monto negativo con
  estatus distinto a `reversada` es un error de datos.
- Los montos estan en la moneda de la fila. Tasas fijas usadas para normalizar a USD (viven en la vista
  `tasas_cambio` de `sql/01_limpieza.sql`): MXN 18.5, COP 4100, BRL 5.4, USD 1.
- "Fecha futura" = `fecha_hora` posterior a 2026-09-30 23:59:59 (fecha de corte de la extraccion).
- Estatus: ~92% aprobada, ~6% rechazada, ~2% reversada.

## Problemas de calidad (con conteos exactos)
| Problema | Filas inyectadas | % del total | Como se detecta |
|---|---:|---:|---|
| Duplicados (misma tarjeta, fecha_hora, monto, comercio; nuevo transaccion_id) | 598 | 0.50% | ROW_NUMBER() por (tarjeta_id, fecha_hora, monto, comercio), rn > 1 |
| `comercio` NULL | 359 | 0.30% | comercio IS NULL |
| Monto negativo que NO es reversa (estatus aprobada/rechazada) | 239 | 0.20% | monto < 0 AND estatus <> 'reversada' |
| `fecha_hora` en el futuro (2026-10-16 a 2027-03-31) | 120 | 0.10% | fecha_hora > '2026-09-30 23:59:59' |

## Anomalias de comportamiento (fraude / outliers)
**Picos de gasto repentinos**: 8 tarjetas, 32 transacciones aprobadas cuyo monto
se multiplico por 10-30x el monto medio historico de la propia tarjeta.
Tarjetas: 808 (5 picos, media USD 299), 880 (4 picos, media USD 327), 628 (3 picos, media USD 363), 645 (4 picos, media USD 164), 631 (4 picos, media USD 317), 633 (3 picos, media USD 413), 331 (5 picos, media USD 1,095), 726 (4 picos, media USD 175).

**Card testing (rafagas de microtransacciones)**: 5 tarjetas, 62 transacciones
nuevas (8-14 por rafaga, montos de USD 0.30-1.50 en 5-8 minutos, madrugada, ~70% rechazadas, comercio de verificacion).
Tarjetas (id, n. de intentos, inicio): 878 (12, 2026-09-20T04:06:10); 807 (14, 2026-09-07T03:21:27); 672 (14, 2026-03-17T00:36:32); 558 (14, 2026-07-11T05:28:19); 656 (8, 2026-07-26T00:36:47).

## Patrones de comportamiento (no son errores)
- Tendencia alcista: el peso diario crece ~25% de inicio a fin de ventana y, ademas, entran clientes/tarjetas nuevos
  durante todo el periodo; el volumen mensual resultante crece ~2.2x entre 2025-10 y 2026-09.
- Estacionalidad de fin de mes: dias >= 26 pesan 1.35x, dias 1-3 pesan 1.10x, dia 15 pesa 1.10x; fines de semana 0.55x.
  En los datos, 22.9% de las transacciones caen en los dias 26-31 (vs ~16.4% si fuera uniforme).
- Churn: ~30% de los clientes deja de transaccionar antes de fin de ventana; tarjetas canceladas/bloqueadas
  dejan de transaccionar antes.
- Montos log-normales por categoria y segmento (multiplicador Startup 0.7 / PyME 1.0 / Corporativo 1.8).

## Nota sobre falsos positivos
Con montos log-normales, la regla de "3 desviaciones estandar" (sql/06, consulta 6.1) marca ~1.9% de las transacciones
aprobadas (2,226 tx en 648 tarjetas) y solo captura ~28 de las 32 filas de pico inyectadas, porque los propios picos
inflan la media y la desviacion (enmascaramiento). El criterio robusto de la consulta 6.2 (>= 3 transacciones por
>= 25x la mediana de la tarjeta) recupera las 8 tarjetas inyectadas con 14 tarjetas marcadas en total.
