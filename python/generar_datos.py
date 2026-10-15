#!/usr/bin/env python3
"""Genera el dataset SINTETICO de pagos con tarjeta corporativa.

Salidas (carpeta data/):
  clientes.csv, tarjetas.csv, transacciones.csv, muestra_transacciones.csv (500 filas),
  anomalias_inyectadas.csv (ground truth, ignorado por git) e INJECTED_ISSUES.md

Reproducible: numpy seed 42. Solo stdlib + numpy + pandas.
Uso:  python python/generar_datos.py
"""
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"
DATA.mkdir(exist_ok=True)

INICIO = pd.Timestamp("2025-10-01")
FIN = pd.Timestamp("2026-09-30")
N_DIAS = (FIN - INICIO).days + 1  # 365
N_CLIENTES = 300
N_TARJETAS = 900
N_BASE = 119_000  # transacciones "limpias" antes de inyectar problemas

# Tasa de cambio fija (unidades de moneda por 1 USD). La misma tabla se usa en sql/01_limpieza.sql
FX = {"MXN": 18.5, "COP": 4100.0, "BRL": 5.4, "USD": 1.0}
MONEDA_LOCAL = {"Mexico": "MXN", "Colombia": "COP", "Brasil": "BRL"}

rng = np.random.default_rng(SEED)

# --------------------------------------------------------------------------- clientes
paises = rng.choice(["Mexico", "Colombia", "Brasil"], size=N_CLIENTES, p=[0.50, 0.25, 0.25])
segmentos = rng.choice(["Startup", "PyME", "Corporativo"], size=N_CLIENTES, p=[0.40, 0.40, 0.20])

# 30% clientes antiguos (2023-01 .. 2025-09), 70% altas dentro de la ventana (2025-10 .. 2026-08)
es_antiguo = rng.random(N_CLIENTES) < 0.30
dias_antiguo = rng.integers(0, (pd.Timestamp("2025-09-30") - pd.Timestamp("2023-01-01")).days + 1, N_CLIENTES)
dias_nuevo = (rng.beta(1.4, 1.0, N_CLIENTES) * (pd.Timestamp("2026-08-31") - INICIO).days).astype(int)
alta = np.where(
    es_antiguo,
    pd.Timestamp("2023-01-01") + pd.to_timedelta(dias_antiguo, "D"),
    INICIO + pd.to_timedelta(dias_nuevo, "D"),
)
alta = pd.to_datetime(alta)

pref = ["Nova", "Altiplano", "Vertice", "Cobalto", "Delta", "Orbita", "Pixel", "Andes", "Sol", "Luna",
        "Rio", "Atlas", "Zenit", "Maya", "Cumbre", "Brisa", "Nube", "Kiwi", "Tigre", "Aguila",
        "Fenix", "Pampa", "Selva", "Costa", "Volcan", "Lago", "Cima", "Raiz", "Ola", "Faro"]
suf = ["Digital", "Logistica", "Soluciones", "Labs", "Group", "Consultores", "Foods", "Media", "Tech",
       "Servicios", "Industrial", "Retail", "Comercial", "Capital", "Studio", "Cloud", "Trade", "Works"]
legal = {"Mexico": "S.A. de C.V.", "Colombia": "S.A.S.", "Brasil": "Ltda."}
nombres, usados = [], set()
while len(nombres) < N_CLIENTES:
    i = len(nombres)
    n = f"{rng.choice(pref)} {rng.choice(suf)} {legal[paises[i]]}"
    if n not in usados:
        usados.add(n)
        nombres.append(n)

clientes = pd.DataFrame({
    "cliente_id": np.arange(1, N_CLIENTES + 1),
    "nombre_empresa": nombres,
    "pais": paises,
    "segmento": segmentos,
    "fecha_alta": alta.strftime("%Y-%m-%d"),
})

# churn: 30% de los clientes deja de transaccionar 2-10 meses despues de entrar a la ventana
tiene_churn = rng.random(N_CLIENTES) < 0.30
inicio_cli = np.maximum(alta.values, np.datetime64(INICIO))
churn_fecha = pd.to_datetime(inicio_cli) + pd.to_timedelta(rng.integers(60, 300, N_CLIENTES), "D")
churn_fecha = pd.to_datetime(np.where(tiene_churn, churn_fecha, FIN))
churn_fecha = churn_fecha.where(churn_fecha <= FIN, FIN)

# --------------------------------------------------------------------------- tarjetas
peso_seg = {"Startup": 1.2, "PyME": 2.0, "Corporativo": 5.0}
w_cli = np.array([peso_seg[s] for s in segmentos]) * rng.lognormal(0, 0.3, N_CLIENTES)
extra = rng.multinomial(N_TARJETAS - N_CLIENTES, w_cli / w_cli.sum())
n_por_cliente = 1 + extra
tj_cliente = np.repeat(np.arange(1, N_CLIENTES + 1), n_por_cliente)
tj_tipo = rng.choice(["fisica", "virtual"], N_TARJETAS, p=[0.45, 0.55])
tj_estatus = rng.choice(["activa", "bloqueada", "cancelada"], N_TARJETAS, p=[0.85, 0.08, 0.07])
alta_tj = alta[tj_cliente - 1]
emision = alta_tj + pd.to_timedelta(rng.integers(0, 90, N_TARJETAS), "D")
emision = emision.where(emision <= pd.Timestamp("2026-09-15"), pd.Timestamp("2026-09-15"))

tarjetas = pd.DataFrame({
    "tarjeta_id": np.arange(1, N_TARJETAS + 1),
    "cliente_id": tj_cliente,
    "tipo": tj_tipo,
    "estatus": tj_estatus,
    "fecha_emision": emision.strftime("%Y-%m-%d"),
})

# ventana activa de cada tarjeta (indices de dia dentro de la ventana de 365 dias)
inicio_tj = np.maximum(emision.values, np.datetime64(INICIO))
fin_tj = churn_fecha[tj_cliente - 1].values.copy()
start_idx = ((pd.to_datetime(inicio_tj) - INICIO).days).values.astype(int)
end_idx = ((pd.to_datetime(fin_tj) - INICIO).days).values.astype(int)
dur_cancel = rng.integers(30, 300, N_TARJETAS)
dur_bloq = rng.integers(60, 330, N_TARJETAS)
end_idx = np.where(tj_estatus == "cancelada", np.minimum(end_idx, start_idx + dur_cancel), end_idx)
end_idx = np.where(tj_estatus == "bloqueada", np.minimum(end_idx, start_idx + dur_bloq), end_idx)
end_idx = np.clip(np.maximum(end_idx, start_idx), 0, N_DIAS - 1)
start_idx = np.clip(start_idx, 0, N_DIAS - 1)

# --------------------------------------------------------------------------- pesos por dia
dias = INICIO + pd.to_timedelta(np.arange(N_DIAS), "D")
w_dia = 1.0 + 0.25 * np.arange(N_DIAS) / (N_DIAS - 1)  # tendencia alcista suave
w_dia = w_dia * np.where(dias.dayofweek >= 5, 0.55, 1.0)  # menos actividad en fin de semana
dm = dias.day.values
w_dia = w_dia * np.where(dm >= 26, 1.35, 1.0) * np.where(dm <= 3, 1.10, 1.0) * np.where(dm == 15, 1.10, 1.0)

# --------------------------------------------------------------------------- transacciones base
act_seg = {"Startup": 0.6, "PyME": 1.0, "Corporativo": 2.5}
seg_tj = segmentos[tj_cliente - 1]
actividad = np.array([act_seg[s] for s in seg_tj]) * rng.lognormal(0, 0.6, N_TARJETAS)
cum_w = np.concatenate([[0.0], np.cumsum(w_dia)])
disp = actividad * (cum_w[end_idx + 1] - cum_w[start_idx])
lam = N_BASE * disp / disp.sum()
n_tx = rng.poisson(lam)

tj_rep, dia_rep = [], []
for i in range(N_TARJETAS):
    if n_tx[i] == 0:
        continue
    rango = np.arange(start_idx[i], end_idx[i] + 1)
    p = w_dia[rango] / w_dia[rango].sum()
    dia_rep.append(rng.choice(rango, size=n_tx[i], p=p))
    tj_rep.append(np.full(n_tx[i], i + 1))
tj_rep = np.concatenate(tj_rep)
dia_rep = np.concatenate(dia_rep)
N = len(tj_rep)

p_hora = np.array([1, 1, 1, 1, 1, 2, 4, 8, 14, 18, 20, 20, 18, 18, 19, 18, 15, 11, 8, 6, 5, 4, 3, 2], float)
p_hora /= p_hora.sum()
horas = rng.choice(24, size=N, p=p_hora)
seg_sec = horas * 3600 + rng.integers(0, 3600, N)
ts = (np.datetime64(str(INICIO.date())) + dia_rep.astype("timedelta64[D]")).astype("datetime64[s]") \
    + seg_sec.astype("timedelta64[s]")

cli_tx = tj_cliente[tj_rep - 1]
seg_tx = segmentos[cli_tx - 1]
pais_cli_tx = paises[cli_tx - 1]

categorias = ["Software", "Viajes", "Publicidad", "Alimentos", "Logistica", "Servicios Profesionales", "Oficina", "Otros"]
mix = {  # mezcla de categorias por segmento
    "Startup":     [0.30, 0.06, 0.24, 0.08, 0.04, 0.10, 0.06, 0.12],
    "PyME":        [0.18, 0.12, 0.16, 0.14, 0.12, 0.10, 0.08, 0.10],
    "Corporativo": [0.14, 0.24, 0.12, 0.08, 0.14, 0.14, 0.06, 0.08],
}
mu_cat = {"Software": 5.0, "Viajes": 5.6, "Publicidad": 5.4, "Alimentos": 3.6, "Logistica": 4.8,
          "Servicios Profesionales": 5.5, "Oficina": 4.2, "Otros": 4.0}
sig_cat = {"Software": 0.8, "Viajes": 1.0, "Publicidad": 0.9, "Alimentos": 0.7, "Logistica": 0.8,
           "Servicios Profesionales": 0.8, "Oficina": 0.7, "Otros": 0.9}
mult_seg = {"Startup": 0.7, "PyME": 1.0, "Corporativo": 1.8}

cat_tx = np.empty(N, dtype=object)
for s in mult_seg:
    m = seg_tx == s
    cat_tx[m] = rng.choice(categorias, size=m.sum(), p=mix[s])

mult_tarjeta = rng.lognormal(0, 0.3, N_TARJETAS)  # "tamano normal" de cada tarjeta
mu = np.array([mu_cat[c] for c in cat_tx]) + np.log([mult_seg[s] for s in seg_tx]) + np.log(mult_tarjeta[tj_rep - 1])
sg = np.array([sig_cat[c] for c in cat_tx])
monto_usd = np.exp(rng.normal(mu, sg))

# moneda y pais del comercio
cross = np.isin(cat_tx, ["Software", "Publicidad", "Viajes"])
es_usd = rng.random(N) < np.where(cross, 0.55, 0.05)
local = np.array([MONEDA_LOCAL[p] for p in pais_cli_tx])
moneda = np.where(es_usd, "USD", local)
pais_ext = rng.choice(["Estados Unidos", "Irlanda", "Paises Bajos", "Singapur"], size=N, p=[0.70, 0.12, 0.10, 0.08])
otro_latam = rng.choice(["Mexico", "Colombia", "Brasil", "Chile", "Peru"], size=N)
pais_com = np.where(es_usd, pais_ext, np.where(rng.random(N) < 0.94, pais_cli_tx, otro_latam))

# comercios por categoria (distribucion tipo Zipf)
comercios = {
    "Software": ["AWS", "Google Workspace", "Microsoft 365", "Slack", "Notion", "GitHub", "Atlassian", "Zoom", "Figma", "HubSpot"],
    "Viajes": ["Aeromexico", "LATAM Airlines", "Avianca", "Volaris", "Booking.com", "Airbnb", "Uber", "Hotel Fiesta Inn", "Copa Airlines", "Despegar"],
    "Publicidad": ["Google Ads", "Meta Ads", "LinkedIn Ads", "TikTok Ads", "Mailchimp", "X Ads", "Taboola", "Semrush"],
    "Alimentos": ["Rappi", "Starbucks", "Oxxo", "Sodexo Comedor", "Uber Eats", "Sanborns", "Juan Valdez", "iFood", "Subway"],
    "Logistica": ["DHL", "FedEx", "Estafeta", "Servientrega", "Mercado Envios", "Correios", "99 Minutos", "Coordinadora"],
    "Servicios Profesionales": ["Deloitte", "Despacho Contable Lopez", "Notaria 14", "Legalzoom", "Upwork", "Fiverr", "Consultoria Andina", "Toptal"],
    "Oficina": ["Office Depot", "Amazon Business", "Staples", "WeWork", "Costco", "Papeleria Dos Mil", "Kalunga", "Homecenter"],
    "Otros": ["Pago Servicios Varios", "Comercio General", "Tienda en Linea", "Suscripcion Mensual", "Donativo ONG", "Parking Plus", "Lavanderia Express"],
}
comercio = np.empty(N, dtype=object)
for c, lista in comercios.items():
    m = cat_tx == c
    p = 1.0 / np.arange(1, len(lista) + 1)
    comercio[m] = rng.choice(lista, size=m.sum(), p=p / p.sum())

# estatus
u = rng.random(N)
p_rech = 0.05 + 0.03 * (monto_usd > 1500)
estatus = np.where(u < p_rech, "rechazada", np.where(u > 1 - 0.018, "reversada", "aprobada")).astype(object)

fx_vec = np.array([FX[m] for m in moneda])


def redondear(monto, mon):
    return np.where(mon == "COP", np.round(monto, 0), np.round(monto, 2))


# las reversadas llevan monto NEGATIVO (contrapartida del cargo original)
signo = np.where(estatus == "reversada", -1.0, 1.0)
monto_usd = monto_usd * signo
monto = redondear(monto_usd * fx_vec, moneda)

base = pd.DataFrame({
    "tarjeta_id": tj_rep, "cliente_id": cli_tx, "fecha_hora": ts, "monto": monto, "moneda": moneda,
    "categoria_comercio": cat_tx, "comercio": comercio, "pais_comercio": pais_com, "estatus": estatus,
    "monto_usd": monto_usd, "tag": "",
})
base = base.reset_index(drop=True)
protegidos = set()

# --------------------------------------------------------------------------- ANOMALIA: picos de gasto
cand = np.where(np.bincount(tj_rep, minlength=N_TARJETAS + 1)[1:] >= 80)[0] + 1
tj_pico = rng.choice(cand, size=8, replace=False)
picos_detalle = []
for t in tj_pico:
    idx_t = base.index[(base.tarjeta_id == t) & (base.estatus == "aprobada")].values
    media_usd = base.loc[idx_t, "monto_usd"].mean()
    k = int(rng.integers(3, 6))
    elegidas = rng.choice(idx_t, size=k, replace=False)
    factores = rng.uniform(10, 30, k)
    for ix, f in zip(elegidas, factores):
        nuevo = media_usd * f
        base.at[ix, "monto_usd"] = nuevo
        base.at[ix, "monto"] = redondear(np.array([nuevo * FX[base.at[ix, "moneda"]]]), np.array([base.at[ix, "moneda"]]))[0]
        base.at[ix, "tag"] = "pico_gasto"
        protegidos.add(ix)
    picos_detalle.append((int(t), k, float(media_usd)))

# --------------------------------------------------------------------------- ANOMALIA: card testing
cand_t = [t for t in np.where(np.bincount(tj_rep, minlength=N_TARJETAS + 1)[1:] >= 30)[0] + 1
          if tj_estatus[t - 1] == "activa" and t not in set(tj_pico.tolist())]
tj_test = rng.choice(cand_t, size=5, replace=False)
filas_test, tests_detalle = [], []
testers = ["PAYVERIFY*TEST", "CHK AUTH 0001", "MICRO-CHARGE ONLINE", "VERIF*CARD", "DIGI SUB TRIAL"]
for t in tj_test:
    d = int(rng.integers(start_idx[t - 1], end_idx[t - 1] + 1))
    h = int(rng.integers(0, 6))
    t0 = np.datetime64(str((INICIO + pd.Timedelta(days=d)).date())) + np.timedelta64(h * 3600 + int(rng.integers(0, 3600)), "s")
    k = int(rng.integers(8, 15))
    offs = np.sort(rng.choice(np.arange(0, int(rng.integers(300, 480))), size=k, replace=False))
    cli = int(tj_cliente[t - 1])
    mon = MONEDA_LOCAL[paises[cli - 1]]
    comer = rng.choice(testers)
    for o in offs:
        usd = rng.uniform(0.3, 1.5)
        filas_test.append({
            "tarjeta_id": int(t), "cliente_id": cli, "fecha_hora": t0 + np.timedelta64(int(o), "s"),
            "monto": float(redondear(np.array([usd * FX[mon]]), np.array([mon]))[0]), "moneda": mon,
            "categoria_comercio": rng.choice(["Otros", "Software"]), "comercio": comer,
            "pais_comercio": "Estados Unidos",
            "estatus": "rechazada" if rng.random() < 0.70 else "aprobada",
            "monto_usd": usd, "tag": "card_testing",
        })
    tests_detalle.append((int(t), k, str(t0)))
test_df = pd.DataFrame(filas_test)
test_df["fecha_hora"] = pd.to_datetime(test_df["fecha_hora"])

# --------------------------------------------------------------------------- PROBLEMAS DE CALIDAD
base["fecha_hora"] = pd.to_datetime(base["fecha_hora"])
n_null = int(round(0.003 * N))
n_neg = int(round(0.002 * N))
n_fut = int(round(0.001 * N))
n_dup = int(round(0.005 * N))

libres = np.array([i for i in base.index if i not in protegidos])
pool_neg = libres[(base.loc[libres, "estatus"] != "reversada").values]
idx_neg = rng.choice(pool_neg, size=n_neg, replace=False)
resto = np.setdiff1d(libres, idx_neg)
resto = rng.permutation(resto)
idx_null, idx_fut, idx_dup = resto[:n_null], resto[n_null:n_null + n_fut], resto[n_null + n_fut:n_null + n_fut + n_dup]

base.loc[idx_neg, "monto"] = -base.loc[idx_neg, "monto"].abs()
base.loc[idx_neg, "monto_usd"] = -base.loc[idx_neg, "monto_usd"].abs()
base.loc[idx_neg, "tag"] = "monto_negativo"
base.loc[idx_null, "comercio"] = None
base.loc[idx_null, "tag"] = "comercio_nulo"
dias_fut = rng.integers(0, (pd.Timestamp("2027-03-31") - pd.Timestamp("2026-10-16")).days + 1, n_fut)
hora_dia = base.loc[idx_fut, "fecha_hora"] - base.loc[idx_fut, "fecha_hora"].dt.normalize()
base.loc[idx_fut, "fecha_hora"] = pd.Timestamp("2026-10-16") + pd.to_timedelta(dias_fut, "D") + hora_dia.values
base.loc[idx_fut, "tag"] = "fecha_futura"

dups = base.loc[idx_dup].copy()
dups["tag"] = "duplicado"

todo = pd.concat([base, test_df, dups], ignore_index=True)
todo = todo.sort_values("fecha_hora", kind="stable").reset_index(drop=True)
todo.insert(0, "transaccion_id", np.arange(1, len(todo) + 1))
todo["fecha_hora"] = todo["fecha_hora"].dt.strftime("%Y-%m-%d %H:%M:%S")

cols = ["transaccion_id", "tarjeta_id", "cliente_id", "fecha_hora", "monto", "moneda",
        "categoria_comercio", "comercio", "pais_comercio", "estatus"]
tx_out = todo[cols].copy()

# --------------------------------------------------------------------------- escritura
clientes.to_csv(DATA / "clientes.csv", index=False)
tarjetas.to_csv(DATA / "tarjetas.csv", index=False)
tx_out.to_csv(DATA / "transacciones.csv", index=False, na_rep="")
tx_out.head(500).to_csv(DATA / "muestra_transacciones.csv", index=False, na_rep="")
todo.loc[todo.tag != "", ["transaccion_id", "tarjeta_id", "tag"]].rename(columns={"tag": "tipo"}) \
    .to_csv(DATA / "anomalias_inyectadas.csv", index=False)

# --------------------------------------------------------------------------- INJECTED_ISSUES.md
cnt = todo.tag.value_counts().to_dict()
total = len(todo)
ok = todo[(todo.tag == "") | (todo.tag == "pico_gasto")]
ok = ok[(ok.estatus == "aprobada")]
ok_mes = ok.assign(mes=ok.fecha_hora.str[:7]).groupby("mes")["monto_usd"].sum()
tx_mes = todo[todo.tag == ""].assign(mes=lambda d: d.fecha_hora.str[:7]).groupby("mes").size()
tx_mes = tx_mes[tx_mes.index <= "2026-09"]
ult = todo[(todo.tag == "") & (todo.fecha_hora <= "2026-09-30 23:59:59")].copy()
ult["dia"] = ult.fecha_hora.str[8:10].astype(int)
fin_mes = (ult.dia >= 26).mean()
dias_por_mes_fin = 5 / 30.4
md = f"""# Problemas y anomalias inyectados (dataset sintetico, seed {SEED})

Dataset generado con `python/generar_datos.py` (numpy seed {SEED}). Total de filas en `transacciones`: **{total:,}**
({len(clientes)} clientes, {len(tarjetas)} tarjetas). Ventana: {INICIO.date()} a {FIN.date()}.
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
| Duplicados (misma tarjeta, fecha_hora, monto, comercio; nuevo transaccion_id) | {cnt.get('duplicado', 0)} | {cnt.get('duplicado', 0) / total:.2%} | ROW_NUMBER() por (tarjeta_id, fecha_hora, monto, comercio), rn > 1 |
| `comercio` NULL | {cnt.get('comercio_nulo', 0)} | {cnt.get('comercio_nulo', 0) / total:.2%} | comercio IS NULL |
| Monto negativo que NO es reversa (estatus aprobada/rechazada) | {cnt.get('monto_negativo', 0)} | {cnt.get('monto_negativo', 0) / total:.2%} | monto < 0 AND estatus <> 'reversada' |
| `fecha_hora` en el futuro (2026-10-16 a 2027-03-31) | {cnt.get('fecha_futura', 0)} | {cnt.get('fecha_futura', 0) / total:.2%} | fecha_hora > '2026-09-30 23:59:59' |

## Anomalias de comportamiento (fraude / outliers)
**Picos de gasto repentinos**: {len(tj_pico)} tarjetas, {cnt.get('pico_gasto', 0)} transacciones aprobadas cuyo monto
se multiplico por 10-30x el monto medio historico de la propia tarjeta.
Tarjetas: {', '.join(f'{t} ({k} picos, media USD {m:,.0f})' for t, k, m in picos_detalle)}.

**Card testing (rafagas de microtransacciones)**: {len(tj_test)} tarjetas, {cnt.get('card_testing', 0)} transacciones
nuevas (8-14 por rafaga, montos de USD 0.30-1.50 en 5-8 minutos, madrugada, ~70% rechazadas, comercio de verificacion).
Tarjetas (id, n. de intentos, inicio): {'; '.join(f'{t} ({k}, {h})' for t, k, h in tests_detalle)}.

## Patrones de comportamiento (no son errores)
- Tendencia alcista: el peso diario crece ~25% de inicio a fin de ventana y, ademas, entran clientes/tarjetas nuevos
  durante todo el periodo; el volumen mensual resultante crece ~2.2x entre 2025-10 y 2026-09.
- Estacionalidad de fin de mes: dias >= 26 pesan 1.35x, dias 1-3 pesan 1.10x, dia 15 pesa 1.10x; fines de semana 0.55x.
  En los datos, {fin_mes:.1%} de las transacciones caen en los dias 26-31 (vs ~{dias_por_mes_fin:.1%} si fuera uniforme).
- Churn: ~30% de los clientes deja de transaccionar antes de fin de ventana; tarjetas canceladas/bloqueadas
  dejan de transaccionar antes.
- Montos log-normales por categoria y segmento (multiplicador Startup 0.7 / PyME 1.0 / Corporativo 1.8).

## Nota sobre falsos positivos
Con montos log-normales, la regla de "3 desviaciones estandar" (sql/06, consulta 6.1) marca ~1.9% de las transacciones
aprobadas (2,226 tx en 648 tarjetas) y solo captura ~28 de las 32 filas de pico inyectadas, porque los propios picos
inflan la media y la desviacion (enmascaramiento). El criterio robusto de la consulta 6.2 (>= 3 transacciones por
>= 25x la mediana de la tarjeta) recupera las 8 tarjetas inyectadas con 14 tarjetas marcadas en total.
"""
(DATA / "INJECTED_ISSUES.md").write_text(md, encoding="utf-8")

print(f"clientes: {len(clientes)}  tarjetas: {len(tarjetas)}  transacciones: {total}")
print({k: v for k, v in cnt.items() if k})
