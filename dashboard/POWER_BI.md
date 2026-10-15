# Rebuilding the dashboard in Power BI

The web dashboard (`docs/index.html`) is the primary deliverable. This guide rebuilds the same views in Power BI
Desktop from the same data, so the numbers match.

<!-- SCREENSHOT PLACEHOLDER: save the finished report as dashboard/powerbi.png, then replace this comment with:
![Power BI dashboard](powerbi.png) -->
_Screenshot coming soon: `dashboard/powerbi.png`._

## 1. Get the data

**Option A — SQLite (recommended, keeps the cleaning logic in SQL).**
Install the [SQLite ODBC driver](http://www.ch-werner.de/sqliteodbc/), create a DSN pointing to
`data/transacciones.db`, then in Power BI: *Get data → ODBC*. Run `python python/exportar_dashboard.py` (or
`python sql/ejecutar_sql.py sql/01_limpieza.sql`) once first so the cleaning views exist in the database. Import:

| Object | Use as |
|---|---|
| `transacciones_limpias` (view) | fact table — already deduplicated, with `monto_usd` and `mes` |
| `clientes` | dimension |
| `tarjetas` | dimension |

**Option B — CSV.** Import `data/transacciones.csv`, `clientes.csv`, `tarjetas.csv` and reproduce the cleaning in
Power Query: remove duplicates on (`tarjeta_id`, `fecha_hora`, `monto`, `comercio`) keeping the lowest
`transaccion_id`; filter out null `comercio`, negative `monto` with `estatus <> "reversada"`, and
`fecha_hora > 2026-09-30 23:59:59`; add `monto_usd = monto / rate` with MXN 18.5, COP 4100, BRL 5.4, USD 1.
Check the row count: 118,951.

## 2. Data model (star schema)

```
dim_fecha     [Date]       1 ──┐
dim_clientes  [cliente_id] 1 ──┼──* fact_transacciones [fecha, cliente_id, tarjeta_id]
dim_tarjetas  [tarjeta_id] 1 ──┘
```

- **fact_transacciones**: one row per cleaned transaction. Add `fecha = DATEVALUE(LEFT(fecha_hora, 10))`.
- **dim_clientes**: `cliente_id`, `pais`, `segmento`, `fecha_alta`. Add a calculated column
  `primer_mes = CALCULATE(MIN(fact_transacciones[fecha]), fact_transacciones[estatus] = "aprobada")` then
  `cohorte = DATE(YEAR([primer_mes]), MONTH([primer_mes]), 1)`.
- **dim_tarjetas**: `tarjeta_id`, `tipo`, `estatus`, `fecha_emision`. Relate it to the fact only (not to clients)
  to avoid an ambiguous path; `fact[cliente_id]` already carries the client.
- **dim_fecha**: `dim_fecha = CALENDAR(DATE(2025,10,1), DATE(2026,9,30))` with `Mes = FORMAT([Date], "YYYY-MM")`,
  `MonthStart = DATE(YEAR([Date]), MONTH([Date]), 1)`, `DiaMes = DAY([Date])`. Mark it as a date table.

All relationships: single direction, one-to-many from the dimension to the fact.

## 3. Key DAX measures

```DAX
Total Volume USD =
CALCULATE ( SUM ( fact_transacciones[monto_usd] ), fact_transacciones[estatus] = "aprobada" )

Approved Tx =
CALCULATE ( COUNTROWS ( fact_transacciones ), fact_transacciones[estatus] = "aprobada" )

Approval Rate =
DIVIDE ( [Approved Tx], COUNTROWS ( fact_transacciones ) )

Avg Ticket = DIVIDE ( [Total Volume USD], [Approved Tx] )

Active Clients =
CALCULATE ( DISTINCTCOUNT ( fact_transacciones[cliente_id] ), fact_transacciones[estatus] = "aprobada" )

Volume Prev Month =
CALCULATE ( [Total Volume USD], DATEADD ( dim_fecha[Date], -1, MONTH ) )

MoM % = DIVIDE ( [Total Volume USD] - [Volume Prev Month], [Volume Prev Month] )
```

**Pareto cumulative %** (use on a table/line visual with `dim_clientes[cliente_id]` sorted by volume):

```DAX
Client Rank =
RANKX ( ALLSELECTED ( dim_clientes[cliente_id] ), [Total Volume USD], , DESC, Dense )

Pareto Cumulative % =
VAR r = [Client Rank]
VAR cum =
    SUMX (
        FILTER ( ALLSELECTED ( dim_clientes[cliente_id] ), [Client Rank] <= r ),
        [Total Volume USD]
    )
RETURN DIVIDE ( cum, CALCULATE ( [Total Volume USD], ALLSELECTED ( dim_clientes[cliente_id] ) ) )

Clients for 80% =
COUNTROWS ( FILTER ( ALLSELECTED ( dim_clientes[cliente_id] ), [Pareto Cumulative %] <= 0.80 ) ) + 1
```

**Retention** (matrix: rows `dim_clientes[cohorte]`, columns a disconnected table `Offsets[k] = GENERATESERIES(0, 11, 1)`):

```DAX
Cohort Size =
CALCULATE ( COUNTROWS ( dim_clientes ), dim_clientes[fecha_alta] >= DATE ( 2025, 10, 1 ) )

Retention % =
VAR k = SELECTEDVALUE ( Offsets[k] )
VAR coh = SELECTEDVALUE ( dim_clientes[cohorte] )
VAR target = EDATE ( coh, k )
VAR active =
    CALCULATE (
        DISTINCTCOUNT ( fact_transacciones[cliente_id] ),
        fact_transacciones[estatus] = "aprobada",
        dim_clientes[fecha_alta] >= DATE ( 2025, 10, 1 ),
        REMOVEFILTERS ( dim_fecha ),
        dim_fecha[MonthStart] = target
    )
RETURN IF ( target <= DATE ( 2026, 9, 1 ), DIVIDE ( active, [Cohort Size] ) )
```

Use a blue single-hue conditional format (background color scale) on `Retention %` to get the heatmap.
Cohorts include only clients onboarded inside the data window, the same rule as the web dashboard and `reports/`.

## 4. Suggested pages

| Page | Visuals |
|---|---|
| **Overview** | KPI cards (Total Volume USD, Approved Tx, Avg Ticket, Approval Rate, Active Clients, MoM %); column chart volume by month; line chart volume by segment; column chart transactions by `DiaMes` highlighting days 26-31 |
| **Segments & Pareto** | Bar charts by segment, country and category; Pareto line (`Pareto Cumulative %` vs client rank) with an 80% constant line |
| **Retention** | Cohort matrix with the heatmap formatting; line of weighted retention by `k` |
| **Data quality** | Card with raw vs clean rows; bar chart of rows removed per rule (import the result of `sql/07_calidad_datos.sql` as a table) |
| **Fraud signals** | Tables from `sql/06_anomalias.sql` (robust spike cards, card-testing bursts); mask card ids with `"****" & RIGHT("0000" & [tarjeta_id], 4)` |

Add slicers for `dim_clientes[pais]` and `dim_clientes[segmento]` and sync them across the first three pages, like the
filters of the web dashboard. Title each visual with the insight (for example "16% of clients generate 80% of volume"),
not just the metric name.
