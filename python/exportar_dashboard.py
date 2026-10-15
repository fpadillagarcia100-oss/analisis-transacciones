#!/usr/bin/env python3
"""Exporta los agregados que consume el dashboard web (docs/index.html).

Salidas:
  docs/data/dashboard.json   agregados (sin filas crudas) para GitHub Pages / servidor local
  docs/data/dashboard.js     el mismo contenido como `window.__DASHBOARD__ = {...}` para que
                             docs/index.html tambien funcione abierto con doble clic (file://)

Que incluye:
  * KPIs, serie mensual, mezcla por segmento / pais / categoria, curva de Pareto, matriz de
    cohortes y distribucion por dia del mes, PRECALCULADOS para cada combinacion de filtros
    (pais x segmento) para que el filtrado en el navegador sea instantaneo.
  * Resumen de calidad de datos (sql/07_calidad_datos.sql sobre la tabla cruda).
  * Senales de fraude: rafagas de card testing (tarjeta enmascarada), tarjetas con picos por
    el criterio robusto (mediana) vs la regla ingenua de 3 sigma, y top de transacciones anomalas.
  * Si existe data/anomalias_inyectadas.csv (ground truth), precision/recall a nivel tarjeta.

Cohorte = primer mes con una transaccion aprobada del cliente; retencion en el mes k = % de
la cohorte con >= 1 transaccion aprobada en el mes cohorte + k.

Uso:  python python/exportar_dashboard.py [--db data/transacciones.db]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
SQL = RAIZ / "sql"
DB_DEFAULT = RAIZ / "data" / "transacciones.db"
GROUND_TRUTH = RAIZ / "data" / "anomalias_inyectadas.csv"
SALIDA = RAIZ / "docs" / "data"

TODOS = "Todos"
PAISES = ["Mexico", "Colombia", "Brasil"]
SEGMENTOS = ["Startup", "PyME", "Corporativo"]

# Reglas de deteccion (mismas que sql/06_anomalias.sql)
MIN_TX_TARJETA = 30          # tarjetas con historia suficiente
Z_UMBRAL = 3.0               # regla ingenua: z-score > 3 sobre media/desviacion de la tarjeta
VECES_MEDIANA = 25.0         # regla robusta: monto >= 25x la mediana de la tarjeta ...
MIN_PICOS = 3                # ... en al menos 3 transacciones
MICRO_USD = 5.0              # card testing: microtransacciones < USD 5 ...
VENTANA_MIN = 10             # ... al menos 5 en 10 minutos en la misma tarjeta
MIN_INTENTOS = 5
MIN_ELEGIBLES = 30           # retencion promedio solo si hay >= 30 clientes elegibles en el mes k


# --------------------------------------------------------------------------- utilidades SQL
def _cargar_ejecutor():
    """Reutiliza sql/ejecutar_sql.py (division de sentencias y ejecucion) en lugar de duplicarlo."""
    spec = importlib.util.spec_from_file_location("ejecutar_sql", SQL / "ejecutar_sql.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EJ = _cargar_ejecutor()


def crear_vistas(con: sqlite3.Connection) -> None:
    """Crea tasas_cambio, transacciones_limpias y transacciones_aprobadas (sql/01_limpieza.sql)."""
    EJ.ejecutar(con, (SQL / "01_limpieza.sql").read_text(encoding="utf-8"), imprimir=False)


def resultados_sql(con: sqlite3.Connection, archivo: str) -> list[pd.DataFrame]:
    """Ejecuta un .sql de la carpeta sql/ y devuelve cada resultado como DataFrame."""
    salida = []
    for sentencia in EJ.sentencias((SQL / archivo).read_text(encoding="utf-8")):
        cur = con.execute(sentencia)
        if cur.description:
            salida.append(pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description]))
    return salida


# --------------------------------------------------------------------------- helpers
def r(x, nd=2):
    """Redondeo seguro para JSON (NaN/inf -> None)."""
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return x
    if not np.isfinite(xf):
        return None
    return round(xf, nd)


def enmascarar(tarjeta_id: int) -> str:
    """El dataset no trae PAN; se enmascara el id interno al estilo ****1234."""
    return f"****{int(tarjeta_id) % 10000:04d}"


def idx_mes(mes: pd.Series) -> pd.Series:
    return mes.str[:4].astype(int) * 12 + mes.str[5:7].astype(int)


# --------------------------------------------------------------------------- bloques por slice
def kpis(limpias: pd.DataFrame, aprob: pd.DataFrame, meses: list[str]) -> dict:
    ult, prev = meses[-1], meses[-2]
    vol_ult = aprob.loc[aprob.mes == ult, "monto_usd"].sum()
    vol_prev = aprob.loc[aprob.mes == prev, "monto_usd"].sum()
    return {
        "volumen_usd": r(aprob.monto_usd.sum(), 0),
        "tx_aprobadas": int(len(aprob)),
        "tx_limpias": int(len(limpias)),
        "ticket_prom_usd": r(aprob.monto_usd.mean() if len(aprob) else None, 2),
        "ticket_mediano_usd": r(aprob.monto_usd.median() if len(aprob) else None, 2),
        "tasa_aprobacion_pct": r(100 * len(aprob) / len(limpias) if len(limpias) else None, 2),
        "clientes_activos_total": int(aprob.cliente_id.nunique()),
        "clientes_activos_ult_mes": int(aprob.loc[aprob.mes == ult, "cliente_id"].nunique()),
        "ult_mes": ult,
        "volumen_ult_mes_usd": r(vol_ult, 0),
        "crecimiento_mom_pct": r(100 * (vol_ult - vol_prev) / vol_prev if vol_prev else None, 2),
    }


def mensual(limpias: pd.DataFrame, aprob: pd.DataFrame, meses: list[str]) -> list[dict]:
    g_all = limpias.groupby("mes").size().reindex(meses, fill_value=0)
    g = aprob.groupby("mes").agg(vol=("monto_usd", "sum"), tx=("monto_usd", "size"),
                                 clientes=("cliente_id", "nunique")).reindex(meses)
    out = []
    for m in meses:
        tx = int(g.at[m, "tx"]) if pd.notna(g.at[m, "tx"]) else 0
        vol = float(g.at[m, "vol"]) if pd.notna(g.at[m, "vol"]) else 0.0
        out.append({
            "mes": m,
            "volumen_usd": r(vol, 0),
            "tx_aprobadas": tx,
            "tx_limpias": int(g_all[m]),
            "ticket_prom_usd": r(vol / tx if tx else None, 2),
            "tasa_aprobacion_pct": r(100 * tx / g_all[m] if g_all[m] else None, 2),
            "clientes_activos": int(g.at[m, "clientes"]) if pd.notna(g.at[m, "clientes"]) else 0,
        })
    for i in range(1, len(out)):
        a, b = out[i - 1]["volumen_usd"], out[i]["volumen_usd"]
        out[i]["crecimiento_mom_pct"] = r(100 * (b - a) / a if a else None, 2)
    out[0]["crecimiento_mom_pct"] = None
    return out


def mensual_por_segmento(aprob: pd.DataFrame, meses: list[str]) -> dict:
    p = aprob.pivot_table(index="mes", columns="segmento", values="monto_usd", aggfunc="sum")
    p = p.reindex(meses).fillna(0)
    return {s: [r(v, 0) for v in p[s]] for s in SEGMENTOS if s in p.columns}


def desglose(aprob: pd.DataFrame, col: str, orden: list[str] | None = None) -> list[dict]:
    g = aprob.groupby(col).agg(volumen_usd=("monto_usd", "sum"), tx=("monto_usd", "size"),
                               clientes=("cliente_id", "nunique"))
    total = g.volumen_usd.sum()
    g = g.sort_values("volumen_usd", ascending=False)
    if orden:
        g = g.reindex([o for o in orden if o in g.index])
    return [{"nombre": k, "volumen_usd": r(v.volumen_usd, 0), "tx": int(v.tx),
             "clientes": int(v.clientes), "ticket_prom_usd": r(v.volumen_usd / v.tx, 2),
             "participacion_pct": r(100 * v.volumen_usd / total, 2)} for k, v in g.iterrows()]


def pareto(aprob: pd.DataFrame) -> dict:
    gasto = aprob.groupby("cliente_id").monto_usd.sum().sort_values(ascending=False).values
    n = len(gasto)
    if n == 0:
        return {"n_clientes": 0, "puntos": [], "umbrales": {}}
    acum = np.cumsum(gasto) / gasto.sum()
    pct_cli = np.arange(1, n + 1) / n
    # curva submuestreada a cada punto porcentual de clientes (max 101 puntos)
    xs = np.linspace(0, 1, 101)
    ys = np.interp(xs, np.concatenate([[0], pct_cli]), np.concatenate([[0], acum]))
    umbrales = {}
    for u in (50, 80, 90):
        k = int(np.argmax(acum >= u / 100) + 1)
        umbrales[str(u)] = {"clientes": k, "pct_clientes": r(100 * k / n, 1)}
    top10 = max(1, int(round(0.10 * n)))
    return {
        "n_clientes": n,
        "puntos": [[r(100 * x, 1), r(100 * y, 2)] for x, y in zip(xs, ys)],
        "umbrales": umbrales,
        "top10pct_participacion": r(100 * acum[top10 - 1], 1),
    }


def dia_del_mes(aprob: pd.DataFrame, inicio: str, fin: str) -> list[dict]:
    calendario = pd.date_range(inicio, fin, freq="D")
    ocurrencias = pd.Series(calendario.day).value_counts().sort_index()  # cuantas veces existe cada dia
    tx = aprob.dia.value_counts().reindex(range(1, 32), fill_value=0)
    prom = tx / ocurrencias.reindex(range(1, 32))
    base = prom.mean()
    return [{"dia": int(d), "tx": int(tx[d]), "tx_prom_diario": r(prom[d], 1),
             "indice": r(100 * prom[d] / base, 1)} for d in range(1, 32)]


def cohortes(aprob: pd.DataFrame, primer_mes_cliente: pd.Series, meses: list[str]) -> dict:
    """Cohorte = primer mes con transaccion aprobada (calculado sobre TODO el dataset, no sobre el
    filtro, para que un cliente no 'nazca' dos veces). Retencion k = activos en cohorte+k / tamano.
    `primer_mes_cliente` ya viene restringido a clientes dados de alta dentro de la ventana: para los
    clientes anteriores el verdadero primer mes no es observable (censura por la izquierda)."""
    n_off = len(meses)
    ult_idx = idx_mes(pd.Series([meses[-1]]))[0]
    act = aprob[["cliente_id", "mes"]].drop_duplicates()
    clientes = act.cliente_id.unique()
    coh = primer_mes_cliente.reindex(clientes).dropna().rename("cohorte").reset_index()
    act = act.merge(coh, on="cliente_id")   # inner join: descarta clientes fuera de cohortes
    act["k"] = idx_mes(act.mes) - idx_mes(act.cohorte)
    filas = []
    for c in meses:
        miembros = coh.loc[coh.cohorte == c, "cliente_id"]
        n = len(miembros)
        if n == 0:
            continue
        ret, cnt = [], []
        c_idx = idx_mes(pd.Series([c]))[0]
        activos_k = act[act.cohorte == c].groupby("k").cliente_id.nunique()
        for k in range(n_off):
            if c_idx + k > ult_idx:
                ret.append(None)
                cnt.append(None)
            else:
                ret.append(r(100 * activos_k.get(k, 0) / n, 1))
                cnt.append(int(activos_k.get(k, 0)))
        filas.append({"cohorte": c, "clientes": int(n), "retencion_pct": ret, "_activos": cnt})
    # promedio ponderado por tamano de cohorte para cada k (solo cohortes con el mes k observable)
    pond = []
    for k in range(n_off):
        num = den = 0
        for f in filas:
            a = f["_activos"][k]
            if a is not None:            # cohorte con el mes k observable
                num += a
                den += f["clientes"]
        pond.append(r(100 * num / den, 1) if den >= MIN_ELEGIBLES else None)
    for f in filas:
        f.pop("_activos")   # evita promedios con muy pocos clientes
    return {"offsets": list(range(n_off)), "filas": filas, "promedio_ponderado_pct": pond,
            "clientes_en_cohortes": int(len(coh)), "min_elegibles": MIN_ELEGIBLES}


# --------------------------------------------------------------------------- anomalias
def deteccion_spikes(aprob: pd.DataFrame) -> tuple[dict, pd.DataFrame, set, set]:
    a = aprob[["transaccion_id", "tarjeta_id", "fecha_hora", "monto_usd", "categoria_comercio",
               "comercio", "segmento", "pais"]].copy()
    g = a.groupby("tarjeta_id").monto_usd
    a["n"] = g.transform("size")
    a["media"] = g.transform("mean")
    a["desv"] = g.transform(lambda s: s.std(ddof=0))   # misma formula poblacional que sql/06
    a["mediana"] = g.transform("median")
    a = a[a.n >= MIN_TX_TARJETA]
    a["z"] = (a.monto_usd - a.media) / a.desv.replace(0, np.nan)
    a["veces_mediana"] = a.monto_usd / a.mediana

    naive = a[a.z > Z_UMBRAL]
    marcadas = a[a.veces_mediana >= VECES_MEDIANA]
    por_tarjeta = marcadas.groupby("tarjeta_id").agg(
        tx_marcadas=("monto_usd", "size"), max_veces_mediana=("veces_mediana", "max"),
        mediana_usd=("mediana", "first"), tx_tarjeta=("n", "first"),
        monto_marcado_usd=("monto_usd", "sum"), segmento=("segmento", "first"), pais=("pais", "first"),
        primera=("fecha_hora", "min"), ultima=("fecha_hora", "max"))
    robustas = por_tarjeta[por_tarjeta.tx_marcadas >= MIN_PICOS].sort_values("monto_marcado_usd", ascending=False)
    tx_robustas = marcadas[marcadas.tarjeta_id.isin(robustas.index)]

    resumen = {
        "tarjetas_evaluadas": int(a.tarjeta_id.nunique()),
        "tx_evaluadas": int(len(a)),
        "naive": {"regla": f"z-score > {Z_UMBRAL:g} vs media/desviacion de la tarjeta",
                  "tx_marcadas": int(len(naive)), "tarjetas_marcadas": int(naive.tarjeta_id.nunique()),
                  "pct_tx": r(100 * len(naive) / len(a), 2)},
        "robusta": {"regla": f">= {MIN_PICOS} transacciones >= {VECES_MEDIANA:g}x la mediana de la tarjeta",
                    "tx_marcadas": int(len(tx_robustas)), "tarjetas_marcadas": int(len(robustas)),
                    "pct_tx": r(100 * len(tx_robustas) / len(a), 3)},
        "tarjetas": [{
            "tarjeta": enmascarar(t), "segmento": v.segmento, "pais": v.pais,
            "tx_tarjeta": int(v.tx_tarjeta), "tx_marcadas": int(v.tx_marcadas),
            "mediana_usd": r(v.mediana_usd, 2), "max_veces_mediana": r(v.max_veces_mediana, 1),
            "monto_marcado_usd": r(v.monto_marcado_usd, 0),
            "primera": str(v.primera)[:10], "ultima": str(v.ultima)[:10], "_tarjeta_id": int(t)}
            for t, v in robustas.iterrows()],
    }
    top = a.sort_values("veces_mediana", ascending=False).head(12)
    ids = {"naive_tx": set(naive.transaccion_id), "robusta_tx": set(tx_robustas.transaccion_id),
           "naive_tj": set(naive.tarjeta_id), "robusta_tj": set(robustas.index)}
    return resumen, top, ids


def rafagas_card_testing(limpias: pd.DataFrame) -> list[dict]:
    p = limpias[(limpias.monto_usd > 0) & (limpias.monto_usd < MICRO_USD)].copy()
    p["ts"] = pd.to_datetime(p.fecha_hora)
    p = p.sort_values(["tarjeta_id", "ts"])
    # nueva rafaga cuando cambia la tarjeta o pasan mas de VENTANA_MIN minutos desde el intento previo
    gap = p.groupby("tarjeta_id").ts.diff().dt.total_seconds().div(60)
    p["rafaga"] = (gap.isna() | (gap > VENTANA_MIN)).cumsum()
    out = []
    for _, b in p.groupby("rafaga"):
        if len(b) < MIN_INTENTOS:
            continue
        dur = (b.ts.max() - b.ts.min()).total_seconds() / 60
        # exigir >= MIN_INTENTOS dentro de alguna ventana de VENTANA_MIN minutos (como sql 6.3)
        t = b.ts.values.astype("datetime64[s]").astype(np.int64)
        max_ventana = max(int(np.searchsorted(t, x + VENTANA_MIN * 60, side="right") - i) for i, x in enumerate(t))
        if max_ventana < MIN_INTENTOS:
            continue
        rech = int((b.estatus == "rechazada").sum())
        out.append({
            "tarjeta": enmascarar(b.tarjeta_id.iloc[0]),
            "inicio": b.ts.min().strftime("%Y-%m-%d %H:%M"),
            "duracion_min": r(dur, 1),
            "intentos": int(len(b)),
            "rechazados": rech,
            "tasa_rechazo_pct": r(100 * rech / len(b), 0),
            "monto_total_usd": r(b.monto_usd.sum(), 2),
            "comercio": b.comercio.mode().iloc[0],
            "segmento": b.segmento.iloc[0],
            "pais": b.pais.iloc[0],
            "_tarjeta_id": int(b.tarjeta_id.iloc[0]),
        })
    return sorted(out, key=lambda x: x["inicio"])


def evaluar_ground_truth(ids: dict, testing_tj: set) -> dict | None:
    if not GROUND_TRUTH.exists():
        return None
    gt = pd.read_csv(GROUND_TRUTH)
    real_picos = set(gt.loc[gt.tipo == "pico_gasto", "tarjeta_id"])
    real_test = set(gt.loc[gt.tipo == "card_testing", "tarjeta_id"])
    real_picos_tx = set(gt.loc[gt.tipo == "pico_gasto", "transaccion_id"])

    def pr(pred: set, real: set, nombre: str) -> dict:
        tp = len(pred & real)
        return {"regla": nombre, "marcadas": len(pred), "reales": len(real), "verdaderos_positivos": tp,
                "precision_pct": r(100 * tp / len(pred) if pred else None, 1),
                "recall_pct": r(100 * tp / len(real) if real else None, 1)}

    return {
        "fuente": "data/anomalias_inyectadas.csv (ground truth del generador)",
        "nivel_tarjeta": [pr(ids["naive_tj"], real_picos, "Spikes: z-score > 3 (naive)"),
                          pr(ids["robusta_tj"], real_picos, "Spikes: >= 3 tx >= 25x median (robust)"),
                          pr(testing_tj, real_test, "Card testing: >= 5 micro-tx in 10 min")],
        "nivel_transaccion": [pr(ids["naive_tx"], real_picos_tx, "Spikes: z-score > 3 (naive)"),
                              pr(ids["robusta_tx"], real_picos_tx, "Spikes: >= 3 tx >= 25x median (robust)")],
    }


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DB_DEFAULT), help="ruta a la base SQLite")
    a = ap.parse_args()
    if not Path(a.db).exists():
        sys.exit(f"No existe {a.db}. Ejecuta python/generar_datos.py y python/cargar_db.py primero.")

    con = sqlite3.connect(a.db)
    crear_vistas(con)

    limpias = pd.read_sql_query(
        """SELECT t.transaccion_id, t.tarjeta_id, t.cliente_id, t.fecha_hora, t.mes, t.monto_usd,
                  t.categoria_comercio, t.comercio, t.estatus, c.pais, c.segmento
           FROM transacciones_limpias t JOIN clientes c ON c.cliente_id = t.cliente_id""", con)
    limpias["dia"] = limpias.fecha_hora.str[8:10].astype(int)
    aprob_all = limpias[limpias.estatus == "aprobada"]
    meses = sorted(limpias.mes.unique())
    inicio, fin = limpias.fecha_hora.min()[:10], limpias.fecha_hora.max()[:10]
    alta = pd.read_sql_query("SELECT cliente_id, fecha_alta FROM clientes", con).set_index("cliente_id").fecha_alta
    en_ventana = alta[alta >= inicio].index            # solo altas dentro de la ventana observable
    primer_mes = aprob_all[aprob_all.cliente_id.isin(en_ventana)].groupby("cliente_id").mes.min()

    # ---------------- slices precalculados (pais x segmento)
    slices = {}
    for pais in [TODOS] + PAISES:
        for seg in [TODOS] + SEGMENTOS:
            m = np.ones(len(limpias), dtype=bool)
            if pais != TODOS:
                m &= (limpias.pais == pais).values
            if seg != TODOS:
                m &= (limpias.segmento == seg).values
            lim = limpias[m]
            ap_ = lim[lim.estatus == "aprobada"]
            slices[f"{pais}|{seg}"] = {
                "kpis": kpis(lim, ap_, meses),
                "mensual": mensual(lim, ap_, meses),
                "mensual_por_segmento": mensual_por_segmento(ap_, meses),
                "por_segmento": desglose(ap_, "segmento", SEGMENTOS),
                "por_pais": desglose(ap_, "pais", PAISES),
                "por_categoria": desglose(ap_, "categoria_comercio"),
                "pareto": pareto(ap_),
                "dia_del_mes": dia_del_mes(ap_, inicio, meses[-1] + "-30"),
                "cohortes": cohortes(ap_, primer_mes, meses),
            }

    # ---------------- calidad de datos (reutiliza sql/07 sobre la tabla cruda)
    cal = resultados_sql(con, "07_calidad_datos.sql")
    problemas, resumen_cal = cal[0], cal[1].iloc[0]
    calidad = {
        "filas_crudas": int(resumen_cal.filas_crudas),
        "filas_limpias": int(resumen_cal.filas_limpias),
        "filas_descartadas": int(resumen_cal.filas_descartadas),
        "pct_utilizable": r(resumen_cal.pct_utilizable, 2),
        "problemas": [{"problema": p.problema, "filas": int(p.filas), "pct": r(p.pct_del_total, 3)}
                      for p in problemas.itertuples()],
        "estatus": {k: int(v) for k, v in limpias.estatus.value_counts().items()},
    }

    # ---------------- anomalias y fraude
    spikes, top, ids = deteccion_spikes(aprob_all)
    robustas_tj = ids["robusta_tj"]
    rafagas = rafagas_card_testing(limpias)
    testing_tj = {x["_tarjeta_id"] for x in rafagas}
    # marca cada alerta como confirmada (o no) por el ground truth, sin exponer el id interno
    gt = pd.read_csv(GROUND_TRUTH) if GROUND_TRUTH.exists() else None
    for lista, tipo in ((rafagas, "card_testing"), (spikes["tarjetas"], "pico_gasto")):
        reales = set(gt.loc[gt.tipo == tipo, "tarjeta_id"]) if gt is not None else None
        for x in lista:
            tid = x.pop("_tarjeta_id")
            x["confirmada_ground_truth"] = (tid in reales) if reales is not None else None
    top_anomalias = [{
        "tarjeta": enmascarar(v.tarjeta_id), "fecha": v.fecha_hora[:16], "categoria": v.categoria_comercio,
        "comercio": v.comercio, "monto_usd": r(v.monto_usd, 2), "mediana_tarjeta_usd": r(v.mediana, 2),
        "veces_mediana": r(v.veces_mediana, 1), "z_score": r(v.z, 2),
        "tarjeta_marcada_robusta": bool(v.tarjeta_id in robustas_tj)} for v in top.itertuples()]

    dashboard = {
        "meta": {
            "titulo": "Corporate card payments analytics",
            "generado_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fuente": "Synthetic dataset (python/generar_datos.py, seed 42) loaded into SQLite",
            "sintetico": True,
            "ventana": {"inicio": inicio, "fin": meses[-1] + "-30"},
            "meses": meses,
            "moneda": "USD (fixed FX: MXN 18.5, COP 4100, BRL 5.4)",
            "definiciones": {
                "volumen": "Sum of approved, cleaned transactions in USD",
                "tasa_aprobacion": "Approved / all cleaned transactions (approved + declined + reversed)",
                "cohorte": "First month with an approved transaction; retention k = share active (>=1 approved tx) in month k",
            },
        },
        "filtros": {"paises": PAISES, "segmentos": SEGMENTOS, "todos": TODOS},
        "slices": slices,
        "calidad": calidad,
        "anomalias": {
            "spikes": spikes,
            "card_testing": {"regla": f">= {MIN_INTENTOS} transacciones < USD {MICRO_USD:g} en {VENTANA_MIN} min",
                             "rafagas": rafagas},
            "top_anomalias": top_anomalias,
            "ground_truth": evaluar_ground_truth(ids, testing_tj),
        },
    }
    con.close()

    SALIDA.mkdir(parents=True, exist_ok=True)
    texto = json.dumps(dashboard, ensure_ascii=False, separators=(",", ":"))
    (SALIDA / "dashboard.json").write_text(texto, encoding="utf-8")
    (SALIDA / "dashboard.js").write_text(f"window.__DASHBOARD__={texto};\n", encoding="utf-8")

    k = slices[f"{TODOS}|{TODOS}"]["kpis"]
    p = slices[f"{TODOS}|{TODOS}"]["pareto"]
    print(f"Escrito {SALIDA / 'dashboard.json'} ({len(texto) / 1024:,.0f} KB, {len(slices)} slices)")
    print(f"  Volumen aprobado: USD {k['volumen_usd']:,.0f} | tx {k['tx_aprobadas']:,} | ticket USD {k['ticket_prom_usd']:,.2f}"
          f" | aprobacion {k['tasa_aprobacion_pct']}% | MoM {k['ult_mes']}: {k['crecimiento_mom_pct']}%")
    print(f"  Pareto: {p['umbrales']['80']['pct_clientes']}% de clientes = 80% del volumen")
    print(f"  Calidad: {calidad['filas_descartadas']:,} filas descartadas ({100 - calidad['pct_utilizable']:.2f}%)")
    print(f"  Spikes ingenua: {spikes['naive']['tx_marcadas']:,} tx / {spikes['naive']['tarjetas_marcadas']} tarjetas;"
          f" robusta: {spikes['robusta']['tarjetas_marcadas']} tarjetas | card testing: {len(rafagas)} rafagas")


if __name__ == "__main__":
    main()
