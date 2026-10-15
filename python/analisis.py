#!/usr/bin/env python3
"""Business analysis of corporate-card payments: what happened and, above all, WHY.

Reads the cleaned views defined in sql/01_limpieza.sql and produces:
    reports/figuras/*.png   presentation-quality charts (150 dpi, one palette)
    reports/hallazgos.md    executive summary (English) + methods + anomaly evaluation
    reports/metricas.json   headline numbers, machine-readable (for the dashboard/README)

Sections:
    1. Monthly volume trend, MoM growth and its decomposition (more clients vs. more spend per client)
    2. End-of-month seasonality (detrended, weekdays only)
    3. Spend mix by segment / country / category, and why Corporate dominates
    4. Client concentration (Pareto / Lorenz)
    5. Activation lag and cohort retention (cohort = first month with an approved transaction)
    6. Ticket-size statistics (log-normal amounts: why the median beats the mean)
    7. Anomaly detection: naive 3-sigma vs robust statistics vs burst rule, evaluated
       against the injected ground truth (data/anomalias_inyectadas.csv)

Usage:
    python python/analisis.py
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless rendering; must run before pyplot is imported
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.ticker import FuncFormatter, MultipleLocator, PercentFormatter  # noqa: E402

from comun import (CATEGORY_LABELS, COUNTRY_LABELS, DB_PATH, FIGURES_DIR, GROUND_TRUTH_PATH,  # noqa: E402
                   REPORTS_DIR, SEGMENT_LABELS, connect, setup_logging)

log = logging.getLogger("analisis")

# --------------------------------------------------------------------------- parameters
WINDOW_START = pd.Timestamp("2025-10-01")
WINDOW_END = pd.Timestamp("2026-09-30")
MIN_CARD_HISTORY = 30          # approved tx a card needs before per-card baselines are trusted
NAIVE_Z_THRESHOLD = 3.0        # classic "3 sigma" rule
ROBUST_Z_THRESHOLD = 3.5       # Iglewicz & Hoaglin (1993) recommended cut-off for modified z
IQR_K = 1.5                    # Tukey fence: Q3 + 1.5 * IQR
CLUSTER_MIN_FLAGS = 2          # card-level rule: >= 2 robust flags on the same card
SQL62_MULTIPLE, SQL62_MIN_TX = 25.0, 3  # phase-1 SQL rule 6.2: >= 3 tx at >= 25x the card median
BURST_MAX_USD = 5.0            # card testing: micro-charges below USD 5 ...
BURST_WINDOW_MIN = 10          # ... at least BURST_MIN_TX of them within 10 minutes
BURST_MIN_TX = 5
MIN_ELIGIBLE_FOR_CURVE = 30    # pooled retention point shown only if >= 30 clients are observable
SEGMENT_ORDER = ["Startup", "PyME", "Corporativo"]

# --------------------------------------------------------------------------- visual identity
# Validated categorical palette (fixed order; colour follows the entity, never its rank).
BLUE, ORANGE, AQUA, YELLOW, VIOLET = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#6250d6"
GREY, GREY_LIGHT = "#9a9893", "#d9d8d4"
INK, INK_2, INK_3 = "#0b0b0b", "#52514e", "#7a7974"
GRID = "#e8e7e3"
SEGMENT_COLORS = {"Startup": AQUA, "PyME": ORANGE, "Corporativo": BLUE}
SEQ_BLUES = LinearSegmentedColormap.from_list(
    "seq_blues", ["#f3f8fe", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"])


def setup_style() -> None:
    """One consistent, recessive style for every figure."""
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Inter", "Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"],
        "font.size": 10,
        "figure.dpi": 150, "savefig.dpi": 150,
        "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
        "axes.edgecolor": GREY_LIGHT, "axes.linewidth": 0.8,
        "axes.labelcolor": INK_2, "axes.labelsize": 10,
        "axes.titlesize": 13, "axes.titleweight": "semibold", "axes.titlecolor": INK,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "axes.grid.axis": "y", "axes.axisbelow": True, "grid.color": GRID, "grid.linewidth": 0.7,
        "xtick.color": INK_2, "ytick.color": INK_2, "xtick.labelsize": 9, "ytick.labelsize": 9,
        "xtick.major.size": 0, "ytick.major.size": 0,
        "legend.frameon": False, "legend.fontsize": 9,
    })


def fmt_usd(x: float, _pos: int | None = None) -> str:
    """Compact USD axis formatter: $950, $12K, $3.4M."""
    a = abs(x)
    if a >= 1e6:
        return f"${x / 1e6:,.1f}M"
    if a >= 1e3:
        return f"${x / 1e3:,.0f}K"
    return f"${x:,.0f}"


def fmt_thousands(x: float, _pos: int | None = None) -> str:
    """Plain thousands separator for counts."""
    return f"{x:,.0f}"


def headline(fig: plt.Figure, title: str, subtitle: str | None = None) -> None:
    """Insight-first title (left aligned) with an optional muted subtitle."""
    fig.text(0.012, 0.975, title, ha="left", va="top", fontsize=13.5, fontweight="semibold", color=INK)
    if subtitle:
        fig.text(0.012, 0.915, subtitle, ha="left", va="top", fontsize=9.5, color=INK_2)


def save(fig: plt.Figure, name: str, top: float = 0.82) -> str:
    """Save a figure into reports/figuras and return its path relative to reports/."""
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    fig.subplots_adjust(top=top)
    path = FIGURES_DIR / name
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("figure written: %s", path.relative_to(REPORTS_DIR.parent))
    return f"figuras/{name}"


def month_index(s: pd.Series) -> pd.Series:
    """'YYYY-MM' (or datetime) -> integer months since year 0, for month arithmetic."""
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.year * 12 + s.dt.month
    return s.str[:4].astype(int) * 12 + s.str[5:7].astype(int)


def month_label(idx: int) -> str:
    """Inverse of month_index: integer month -> 'YYYY-MM'."""
    year, month0 = divmod(int(idx) - 1, 12)
    return f"{year}-{month0 + 1:02d}"


# --------------------------------------------------------------------------- data
@dataclass
class Data:
    """Frames used throughout the analysis."""

    clean: pd.DataFrame      # transacciones_limpias + client attributes (all statuses)
    approved: pd.DataFrame   # approved subset: the basis of spend/volume
    clients: pd.DataFrame
    cards: pd.DataFrame


def load_data(con: sqlite3.Connection) -> Data:
    """Load the cleaned transactions joined with client attributes."""
    clean = pd.read_sql_query(
        """
        SELECT t.transaccion_id, t.tarjeta_id, t.cliente_id, t.fecha_hora, t.mes, t.monto_usd,
               t.categoria_comercio, t.comercio, t.estatus, c.segmento, c.pais, c.fecha_alta
        FROM transacciones_limpias t
        JOIN clientes c ON c.cliente_id = t.cliente_id
        """, con)
    clean["fecha_hora"] = pd.to_datetime(clean["fecha_hora"])
    clients = pd.read_sql_query("SELECT * FROM clientes", con)
    clients["fecha_alta"] = pd.to_datetime(clients["fecha_alta"])
    cards = pd.read_sql_query("SELECT * FROM tarjetas", con)
    approved = clean[clean["estatus"] == "aprobada"].copy()
    return Data(clean=clean, approved=approved, clients=clients, cards=cards)


def load_ground_truth(path: Path = GROUND_TRUTH_PATH) -> pd.DataFrame:
    """Row-level ground truth of the injected anomalies (transaccion_id, tarjeta_id, tipo)."""
    return pd.read_csv(path)


# --------------------------------------------------------------------------- 1. trend
def monthly_trend(d: Data) -> dict:
    """Monthly approved volume, MoM growth and a clients x spend-per-client decomposition."""
    a = d.approved
    m = a.groupby("mes").agg(volume_usd=("monto_usd", "sum"), approved_tx=("monto_usd", "size"),
                             active_clients=("cliente_id", "nunique"))
    m["tx"] = d.clean.groupby("mes").size()
    m["spend_per_client"] = m["volume_usd"] / m["active_clients"]
    m["mom_pct"] = 100 * m["volume_usd"].pct_change()
    first, last = m.iloc[0], m.iloc[-1]
    growth = last["volume_usd"] / first["volume_usd"]
    clients_growth = last["active_clients"] / first["active_clients"]
    spc_growth = last["spend_per_client"] / first["spend_per_client"]
    # log decomposition: ln(V1/V0) = ln(C1/C0) + ln(S1/S0)
    share_clients = np.log(clients_growth) / np.log(growth)
    mom = m["mom_pct"].dropna()
    return {
        "monthly": m.reset_index(),
        "first_month": m.index[0], "last_month": m.index[-1],
        "first_volume": float(first["volume_usd"]), "last_volume": float(last["volume_usd"]),
        "growth_multiple": float(growth),
        "avg_mom_pct": float(mom.mean()), "median_mom_pct": float(mom.median()),
        "cmgr_pct": float(100 * (growth ** (1 / (len(m) - 1)) - 1)),
        "negative_mom_months": int((mom < 0).sum()),
        "first_clients": int(first["active_clients"]), "last_clients": int(last["active_clients"]),
        "clients_growth": float(clients_growth), "spend_per_client_growth": float(spc_growth),
        "first_spend_per_client": float(first["spend_per_client"]),
        "last_spend_per_client": float(last["spend_per_client"]),
        "share_growth_from_clients_pct": float(100 * share_clients),
        "total_volume": float(a["monto_usd"].sum()), "total_approved_tx": int(len(a)),
    }


def plot_trend(t: dict) -> str:
    m = t["monthly"]
    labels = pd.to_datetime(m["mes"]).dt.strftime("%b\n%Y")
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 5.2), gridspec_kw={"width_ratios": [1.6, 1]})
    x = np.arange(len(m))
    ax1.bar(x, m["volume_usd"], color=BLUE, width=0.72)
    ax1.set_xticks(x, labels)
    ax1.yaxis.set_major_formatter(FuncFormatter(fmt_usd))
    ax1.set_ylabel("Approved volume (USD)")
    ax1.set_title("Monthly approved volume and MoM growth", loc="left", fontsize=10.5, color=INK_2)
    for i, (v, g) in enumerate(zip(m["volume_usd"], m["mom_pct"])):
        if pd.notna(g):
            ax1.text(i, v * 1.012, f"{g:+.0f}%", ha="center", va="bottom", fontsize=7.5,
                     color=ORANGE if g < 0 else INK_2)
    ax1.set_ylim(0, m["volume_usd"].max() * 1.12)

    # Indexed to the first month (=100) on ONE axis: avoids a misleading dual-axis chart.
    base = m.iloc[0]
    ax2.plot(x, 100 * m["active_clients"] / base["active_clients"], color=BLUE, lw=2, marker="o", ms=4,
             label="Active clients")
    ax2.plot(x, 100 * m["spend_per_client"] / base["spend_per_client"], color=ORANGE, lw=2, marker="o", ms=4,
             label="Spend per active client")
    ax2.axhline(100, color=GREY, lw=0.8)
    ax2.set_xticks(x[::2], labels[::2])
    ax2.set_ylabel(f"Index ({pd.Timestamp(t['first_month']).strftime('%b %Y')} = 100)")
    ax2.set_title("Growth drivers (indexed)", loc="left", fontsize=10.5, color=INK_2)
    for col, color in (("active_clients", BLUE), ("spend_per_client", ORANGE)):
        yv = 100 * m[col].iloc[-1] / base[col]
        ax2.annotate(f"{yv:.0f}", (x[-1], yv), xytext=(6, 0), textcoords="offset points",
                     va="center", fontsize=9, color=INK, fontweight="semibold")
    ax2.legend(loc="upper left")
    ax2.set_xlim(-0.5, len(m) - 0.2)
    spc_change = 100 * (t["spend_per_client_growth"] - 1)
    headline(fig, f"Volume grew {t['growth_multiple']:.1f}x in 12 months, all of it from new clients",
             f"Active clients x{t['clients_growth']:.2f}, while spend per active client changed {spc_change:+.0f}%: "
             "a growing share of the base is new clients who have not ramped up yet (next chart).")
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    return save(fig, "01_tendencia_mensual.png", top=0.80)


def client_ramp(d: Data) -> dict:
    """How spend per client builds up after the first transaction (new clients only).

    Explains why spend per *active* client can fall while volume grows: every month
    a large share of active clients are new and still ramping up (cards are activated
    gradually). Tenure k = months since the client's first approved transaction;
    month 0 is usually a partial month. Legacy clients (signed up before the window)
    are the control group: their spend per client should be stable.
    """
    a = d.approved
    clients = d.clients.set_index("cliente_id")
    cm = a.groupby(["cliente_id", "mes"]).agg(spend=("monto_usd", "sum"), cards=("tarjeta_id", "nunique"))
    cm = cm.reset_index()
    cm["new"] = cm["cliente_id"].map(clients["fecha_alta"] >= WINDOW_START)
    cm["m"] = month_index(cm["mes"])
    cm["k"] = cm["m"] - cm.groupby("cliente_id")["m"].transform("min")

    new = cm[cm["new"]]
    ramp = new.groupby("k").agg(median_spend=("spend", "median"), mean_cards=("cards", "mean"),
                                client_months=("spend", "size"))
    ramp = ramp[ramp["client_months"] >= MIN_ELIGIBLE_FOR_CURVE]
    steady = new[new["k"] >= 3]
    steady_spend = float(steady["spend"].median())
    steady_cards = float(steady["cards"].mean())

    legacy = cm[~cm["new"]].groupby("mes")["spend"].mean()
    last = cm[cm["mes"] == cm["mes"].max()]
    return {
        "ramp": ramp, "steady_median_spend": steady_spend, "steady_mean_cards": steady_cards,
        "m0_pct_of_steady": 100 * float(ramp.loc[0, "median_spend"]) / steady_spend,
        "m1_pct_of_steady": 100 * float(ramp.loc[1, "median_spend"]) / steady_spend,
        "m2_pct_of_steady": 100 * float(ramp.loc[2, "median_spend"]) / steady_spend,
        "m0_cards": float(ramp.loc[0, "mean_cards"]),
        "legacy_spend_growth": float(legacy.iloc[-1] / legacy.iloc[0]),
        "legacy_first_spend": float(legacy.iloc[0]), "legacy_last_spend": float(legacy.iloc[-1]),
        "pct_active_in_ramp_last_month": float(100 * (last["new"] & (last["k"] <= 2)).mean()),
        "peak_cards_month": int(ramp["mean_cards"].idxmax()), "peak_cards": float(ramp["mean_cards"].max()),
        "last_ramp_month": int(ramp.index.max()), "last_ramp_cards": float(ramp["mean_cards"].iloc[-1]),
    }


def plot_ramp(rp: dict) -> str:
    ramp = rp["ramp"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.8))
    x = ramp.index.to_numpy()
    colors = [GREY_LIGHT if k == 0 else (BLUE if k >= 3 else "#86b6ef") for k in x]
    ax1.bar(x, ramp["median_spend"], color=colors, width=0.7)
    ax1.axhline(rp["steady_median_spend"], color=INK_3, lw=0.9)
    ax1.text(x[-1] + 0.45, rp["steady_median_spend"], "steady\nstate", va="center", fontsize=8, color=INK_3)
    for k, v in zip(x, ramp["median_spend"]):
        ax1.text(k, v * 1.02, f"{100 * v / rp['steady_median_spend']:.0f}%", ha="center", va="bottom", fontsize=8.5,
                 color=INK)
    ax1.text(0, ramp["median_spend"].iloc[0] * 0.5, "partial\nmonth", ha="center", fontsize=8, color=INK_2)
    ax1.yaxis.set_major_formatter(FuncFormatter(fmt_usd))
    ax1.set_xticks(x)
    ax1.set_xlim(-0.6, x[-1] + 1.1)
    ax1.set_xlabel("Months since first approved transaction")
    ax1.set_ylabel("Median monthly spend per client (USD)")
    ax1.set_title("Spend per new client (% of steady state = months 3+)", loc="left", fontsize=10.5, color=INK_2)

    ax2.plot(x, ramp["mean_cards"], color=BLUE, lw=2.2, marker="o", ms=5)
    for k, v in zip(x, ramp["mean_cards"]):
        ax2.text(k, v + 0.06, f"{v:.1f}", ha="center", va="bottom", fontsize=8.5, color=INK)
    ax2.set_ylim(0, ramp["mean_cards"].max() * 1.25)
    ax2.set_xticks(x)
    ax2.set_xlabel("Months since first approved transaction")
    ax2.set_ylabel("Cards with >= 1 approved transaction")
    ax2.set_title("Active cards per new client", loc="left", fontsize=10.5, color=INK_2)
    headline(fig, f"New clients need about three months to reach full spend; in the first full month they spend "
                  f"{rp['m1_pct_of_steady']:.0f}%",
             f"Cards are activated gradually ({rp['m0_cards']:.1f} -> {rp['steady_mean_cards']:.1f} active cards). Legacy "
             f"clients' spend per client is stable (x{rp['legacy_spend_growth']:.2f}), so the drop in the average is a "
             "mix effect, not lost wallet share.")
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    return save(fig, "02_rampa_nuevos_clientes.png", top=0.80)


# --------------------------------------------------------------------------- 2. seasonality
def month_end_seasonality(d: Data) -> dict:
    """Detrended day-of-month index on weekdays (removes growth trend and weekend effect).

    For every weekday we divide its approved volume by the mean weekday volume of
    its own month (so the growth trend cancels out); then we average that ratio
    per day of month. 1.00 = a typical weekday of that month.
    """
    a = d.approved
    daily = a.groupby(a["fecha_hora"].dt.normalize())["monto_usd"].agg(["sum", "size"])
    full = pd.date_range(WINDOW_START, WINDOW_END, freq="D")
    daily = daily.reindex(full, fill_value=0)
    daily.index.name = "day"
    daily = daily.reset_index()
    daily["weekday"] = daily["day"].dt.dayofweek < 5
    daily["month"] = daily["day"].dt.to_period("M")
    daily["dom"] = daily["day"].dt.day
    wd = daily[daily["weekday"]].copy()
    wd["ratio"] = wd["sum"] / wd.groupby("month")["sum"].transform("mean")
    idx = wd.groupby("dom")["ratio"].mean()

    def band(lo: int, hi: int, exclude: tuple[int, ...] = ()) -> float:
        sel = wd[(wd["dom"] >= lo) & (wd["dom"] <= hi) & ~wd["dom"].isin(exclude)]
        return float(sel["ratio"].mean())

    mid = band(4, 25, exclude=(15,))
    end = band(26, 31)
    start = band(1, 3)
    # share of all approved transactions on days 26-31 vs the calendar share of those days
    share_tx = float((a["fecha_hora"].dt.day >= 26).mean())
    cal_share = float((daily["dom"] >= 26).mean())
    weekend_ratio = float(daily.loc[~daily["weekday"], "size"].mean() / daily.loc[daily["weekday"], "size"].mean())
    return {
        "index_by_day": idx, "end_vs_mid": end / mid, "start_vs_mid": start / mid,
        "day15_vs_mid": float(idx.loc[15] / mid), "share_tx_days_26_31": share_tx,
        "calendar_share_days_26_31": cal_share, "weekend_vs_weekday_tx": weekend_ratio,
        "mid_index": mid,
    }


def plot_seasonality(s: dict) -> str:
    idx = s["index_by_day"] / s["mid_index"]  # re-base so that mid-month weekdays = 1.00
    fig, ax = plt.subplots(figsize=(11, 4.8))
    colors = [ORANGE if day >= 26 else (BLUE if day <= 3 or day == 15 else GREY_LIGHT) for day in idx.index]
    ax.bar(idx.index, idx.values, color=colors, width=0.75)
    ax.axhline(1.0, color=INK_3, lw=0.9)
    ax.text(31.6, 1.0, "typical\nweekday", va="center", fontsize=8, color=INK_3)
    ax.set_xticks(range(1, 32))
    ax.set_xlim(0.3, 33)
    ax.set_ylim(0, idx.max() * 1.15)
    ax.set_xlabel("Day of month")
    ax.set_ylabel("Volume vs. a mid-month weekday (1.00 = same)")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.2f}x"))
    ax.text(28.5, idx.loc[26:31].max() * 1.05, f"Days 26-31: {s['end_vs_mid']:.2f}x", ha="center",
            fontsize=9.5, color=INK, fontweight="semibold")
    ax.text(2, idx.loc[1:3].max() * 1.05, f"Days 1-3: {s['start_vs_mid']:.2f}x", ha="center", fontsize=9, color=INK)
    ax.text(15, idx.loc[15] * 1.05, f"Day 15: {s['day15_vs_mid']:.2f}x", ha="center", fontsize=9, color=INK)
    headline(fig, f"Month-end closing drives spend: days 26-31 run {100 * (s['end_vs_mid'] - 1):.0f}% "
                  f"above a mid-month weekday",
             f"{s['share_tx_days_26_31']:.1%} of approved transactions fall on days 26-31, which are only "
             f"{s['calendar_share_days_26_31']:.1%} of calendar days. Weekdays only, each day indexed to its month's "
             "average (trend removed).")
    fig.tight_layout(rect=(0, 0, 1, 0.84))
    return save(fig, "03_estacionalidad_fin_de_mes.png", top=0.80)


# --------------------------------------------------------------------------- 3. spend mix
def spend_mix(d: Data) -> dict:
    """Spend by segment/country/category, plus a multiplicative decomposition of spend per client.

    spend per client = cards per client x approved tx per card x mean ticket
    (the mean is the right statistic here because totals = count x mean).
    """
    a = d.approved
    total = a["monto_usd"].sum()
    n_clients = d.clients.groupby("segmento").size()
    seg = a.groupby("segmento").agg(spend=("monto_usd", "sum"), tx=("monto_usd", "size"),
                                    active_clients=("cliente_id", "nunique"), active_cards=("tarjeta_id", "nunique"))
    seg["clients"] = n_clients
    seg = seg.reindex(SEGMENT_ORDER)
    seg["spend_share"] = seg["spend"] / total
    seg["client_share"] = seg["clients"] / seg["clients"].sum()
    seg["cards_per_client"] = seg["active_cards"] / seg["active_clients"]
    seg["tx_per_card"] = seg["tx"] / seg["active_cards"]
    seg["mean_ticket"] = seg["spend"] / seg["tx"]
    seg["spend_per_client"] = seg["spend"] / seg["active_clients"]
    country = a.groupby("pais")["monto_usd"].sum().sort_values(ascending=False)
    category = a.groupby("categoria_comercio")["monto_usd"].sum().sort_values(ascending=False)
    corp_cat = a[a["segmento"] == "Corporativo"].groupby("categoria_comercio")["monto_usd"].sum()
    corp_cat = (corp_cat / corp_cat.sum()).sort_values(ascending=False)
    return {"segments": seg, "country_share": country / total, "category_share": category / total,
            "corporate_category_share": corp_cat, "total": float(total)}


def plot_segments(mx: dict) -> str:
    seg = mx["segments"]
    corp = seg.loc["Corporativo"]
    st = seg.loc["Startup"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.8), gridspec_kw={"width_ratios": [1, 1.25]})
    y = np.arange(len(seg))[::-1]
    h = 0.36
    ax1.barh(y + h / 2, seg["client_share"], height=h, color=GREY_LIGHT, label="Share of clients")
    ax1.barh(y - h / 2, seg["spend_share"], height=h, color=BLUE, label="Share of spend")
    for yi, cs, ss in zip(y, seg["client_share"], seg["spend_share"]):
        ax1.text(cs + 0.01, yi + h / 2, f"{cs:.0%}", va="center", fontsize=8.5, color=INK_2)
        ax1.text(ss + 0.01, yi - h / 2, f"{ss:.0%}", va="center", fontsize=8.5, color=INK, fontweight="semibold")
    ax1.set_yticks(y, [SEGMENT_LABELS[s] for s in seg.index])
    ax1.xaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax1.set_xlim(0, max(seg["spend_share"].max(), seg["client_share"].max()) * 1.2)
    ax1.grid(axis="y", visible=False)
    ax1.grid(axis="x", visible=True)
    ax1.legend(loc="upper right")
    ax1.set_title("Clients vs. spend", loc="left", fontsize=10.5, color=INK_2)

    # Why: each driver for Corporate relative to Startup (multiplicative).
    drivers = ["cards_per_client", "tx_per_card", "mean_ticket", "spend_per_client"]
    names = ["Cards per client", "Transactions per card", "Mean ticket", "= Spend per client"]
    ratios = [corp[k] / st[k] for k in drivers]
    yy = np.arange(len(drivers))[::-1]
    ax2.barh(yy, ratios, color=[BLUE, BLUE, BLUE, ORANGE], height=0.55)
    for yi, r in zip(yy, ratios):
        ax2.text(r + 0.15, yi, f"{r:.1f}x", va="center", fontsize=9.5, color=INK, fontweight="semibold")
    ax2.axvline(1, color=INK_3, lw=0.8)
    ax2.set_yticks(yy, names)
    ax2.set_xlim(0, max(ratios) * 1.15)
    ax2.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}x"))
    ax2.grid(axis="y", visible=False)
    ax2.grid(axis="x", visible=True)
    ax2.set_title("Why: Corporate vs. Startup, per active client", loc="left", fontsize=10.5, color=INK_2)
    headline(fig, f"Corporate clients are {corp['client_share']:.0%} of the base but {corp['spend_share']:.0%} "
                  "of spend",
             "The gap compounds: more cards per client, more transactions per card and a larger ticket "
             "multiply into a much larger wallet.")
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    return save(fig, "04_segmentos.png", top=0.80)


def plot_category_country(mx: dict) -> str:
    cat = mx["category_share"]
    cty = mx["country_share"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.8), gridspec_kw={"width_ratios": [1.6, 1]})
    y = np.arange(len(cat))[::-1]
    top3 = set(cat.index[:3])
    ax1.barh(y, cat.values, color=[BLUE if c in top3 else GREY_LIGHT for c in cat.index], height=0.62)
    for yi, v in zip(y, cat.values):
        ax1.text(v + 0.004, yi, f"{v:.1%}", va="center", fontsize=8.5, color=INK)
    ax1.set_yticks(y, [CATEGORY_LABELS[c] for c in cat.index])
    ax1.xaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax1.set_xlim(0, cat.max() * 1.18)
    ax1.grid(axis="y", visible=False)
    ax1.grid(axis="x", visible=True)
    ax1.set_title("Share of spend by merchant category", loc="left", fontsize=10.5, color=INK_2)
    y2 = np.arange(len(cty))[::-1]
    ax2.barh(y2, cty.values, color=BLUE, height=0.5)
    for yi, v in zip(y2, cty.values):
        ax2.text(v + 0.01, yi, f"{v:.1%}", va="center", fontsize=8.5, color=INK)
    ax2.set_yticks(y2, [COUNTRY_LABELS[c] for c in cty.index])
    ax2.xaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax2.set_xlim(0, cty.max() * 1.2)
    ax2.grid(axis="y", visible=False)
    ax2.grid(axis="x", visible=True)
    ax2.set_title("Share of spend by client country", loc="left", fontsize=10.5, color=INK_2)
    top3_share = cat.iloc[:3].sum()
    headline(fig, f"Three categories ({', '.join(CATEGORY_LABELS[c] for c in cat.index[:3])}) "
                  f"capture {top3_share:.0%} of spend",
             f"{COUNTRY_LABELS[cty.index[0]]} accounts for {cty.iloc[0]:.0%} of spend. Amounts converted to USD at "
             "fixed rates (sql/01_limpieza.sql).")
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    return save(fig, "05_categorias_paises.png", top=0.80)


# --------------------------------------------------------------------------- 4. pareto
def pareto(d: Data) -> dict:
    """Client concentration: how many clients make up 50/80/90% of spend, plus the Gini index."""
    spend = d.approved.groupby("cliente_id")["monto_usd"].sum().sort_values(ascending=False)
    cum = spend.cumsum() / spend.sum()
    n = len(spend)
    need = {p: int((cum < p / 100).sum() + 1) for p in (50, 80, 90)}
    # Gini from the Lorenz curve (ascending order)
    asc = np.sort(spend.values)
    lorenz = np.cumsum(asc) / asc.sum()
    gini = float(1 - 2 * np.trapezoid(np.r_[0, lorenz], dx=1 / n))
    seg = d.clients.set_index("cliente_id")["segmento"]
    top80 = spend.index[: need[80]]
    top80_mix = seg.loc[top80].value_counts(normalize=True).reindex(SEGMENT_ORDER, fill_value=0)
    return {"spend_sorted": spend, "cum_share": cum, "clients_with_spend": n, "clients_needed": need,
            "pct_clients_needed": {p: 100 * v / n for p, v in need.items()},
            "top10_share": float(cum.iloc[9]), "gini": gini, "top80_segment_mix": top80_mix}


def plot_pareto(p: dict) -> str:
    cum = p["cum_share"].values
    n = len(cum)
    x = np.arange(1, n + 1) / n
    fig, ax = plt.subplots(figsize=(10, 5.2))
    ax.plot(np.r_[0, x], np.r_[0, cum], color=BLUE, lw=2.2, label="Cumulative share of spend")
    ax.plot([0, 1], [0, 1], color=GREY, lw=0.9, label="Perfect equality")
    k = p["clients_needed"][80]
    ax.plot([k / n, k / n], [0, 0.8], color=ORANGE, lw=1.1)
    ax.plot([0, k / n], [0.8, 0.8], color=ORANGE, lw=1.1)
    ax.scatter([k / n], [0.8], s=40, color=ORANGE, zorder=3, edgecolor="white", linewidth=1.5)
    ax.annotate(f"{k} clients ({p['pct_clients_needed'][80]:.0f}% of clients)\n= 80% of spend",
                (k / n, 0.8), xytext=(18, -40), textcoords="offset points", fontsize=9.5, color=INK,
                arrowprops={"arrowstyle": "-", "color": INK_3, "lw": 0.8})
    ax.xaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("Clients, ranked by spend (cumulative %)")
    ax.set_ylabel("Cumulative share of approved spend")
    ax.grid(axis="x", visible=True)
    ax.legend(loc="lower right")
    mix = p["top80_segment_mix"]
    headline(fig, f"{p['pct_clients_needed'][80]:.0f}% of clients generate 80% of spend "
                  f"(top 10 alone: {p['top10_share']:.0%})",
             f"Gini = {p['gini']:.2f}. The 80% group is {mix['Corporativo']:.0%} Corporate, {mix['PyME']:.0%} SME, "
             f"{mix['Startup']:.0%} Startup.")
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    return save(fig, "06_pareto_clientes.png", top=0.82)


# --------------------------------------------------------------------------- 5. cohorts
def activation_and_retention(d: Data) -> dict:
    """Activation lag and cohort retention with a sound cohort definition.

    Cohort = calendar month of the client's FIRST approved transaction, restricted to
    clients who signed up inside the observation window (for older clients the true
    first month is unobservable: left-censoring). Retention at month k = share of the
    cohort with >= 1 approved transaction in calendar month (cohort + k). A cohort only
    contributes to month k if that month is observable (right-censoring), and the
    pooled curve is the eligibility-weighted average across cohorts.

    Why not cohort = signup month (phase-1 SQL)? Cards are issued days to weeks after
    signup, so many clients have not transacted yet in their signup month. Month 0/1
    then measure *activation*, not retention, and the curve rises over time.
    """
    a = d.approved
    clients = d.clients.set_index("cliente_id")
    new_ids = clients.index[clients["fecha_alta"] >= WINDOW_START]

    first_tx = a.groupby("cliente_id")["fecha_hora"].min()
    new_first = first_tx.reindex(new_ids)
    lag_days = (new_first - clients.loc[new_ids, "fecha_alta"]).dt.days
    same_month = (month_index(new_first.dropna()) == month_index(clients.loc[new_first.dropna().index,
                                                                              "fecha_alta"]))
    activation = {
        "new_clients": int(len(new_ids)),
        "never_transacted": int(new_first.isna().sum()),
        "median_days_to_first_tx": float(lag_days.median()),
        "p75_days_to_first_tx": float(lag_days.quantile(0.75)),
        "pct_first_tx_in_signup_month": float(100 * same_month.mean()),
        "pct_activated_within_30d": float(100 * (lag_days <= 30).mean()),
    }

    # First card issued per client: explains the activation lag.
    first_card = pd.to_datetime(d.cards.groupby("cliente_id")["fecha_emision"].min()).reindex(new_ids)
    activation["median_days_signup_to_first_card"] = float(
        (first_card - clients.loc[new_ids, "fecha_alta"]).dt.days.median())

    last_m = WINDOW_END.year * 12 + WINDOW_END.month
    act = a.loc[a["cliente_id"].isin(new_ids), ["cliente_id", "mes"]].drop_duplicates()
    act["m"] = month_index(act["mes"])

    # Corrected definition: cohort = first month with an approved transaction.
    first_m = act.groupby("cliente_id")["m"].min()
    matrix, size, curve_df = retention_table(act, first_m, last_m)
    # Phase-1 definition, kept only to document the bias: cohort = signup month (includes clients
    # who have not transacted yet, so early months measure activation).
    signup_m = month_index(clients.loc[new_ids, "fecha_alta"])
    _, _, legacy_curve = retention_table(act, signup_m, last_m)

    reliable = curve_df[curve_df["eligible_clients"] >= MIN_ELIGIBLE_FOR_CURVE]
    return {"activation": activation, "matrix": matrix, "cohort_size": size, "curve": curve_df,
            "curve_reliable": reliable,
            "legacy_curve": legacy_curve[legacy_curve["eligible_clients"] >= MIN_ELIGIBLE_FOR_CURVE],
            "m1": float(curve_df.loc[1, "retention_pct"]), "m3": float(curve_df.loc[3, "retention_pct"]),
            "m6": float(curve_df.loc[6, "retention_pct"]),
            "legacy": {f"m{k}": float(legacy_curve.loc[k, "retention_pct"]) for k in (0, 1, 3, 6)},
            "last_reliable_month": int(reliable["month"].max()),
            "last_reliable_pct": float(reliable["retention_pct"].iloc[-1])}


def retention_table(act: pd.DataFrame, cohort_of: pd.Series,
                    last_m: int) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Cohort retention matrix and pooled curve.

    Args:
        act: one row per (cliente_id, active month) with integer month column ``m``.
        cohort_of: integer cohort month per cliente_id (index). Every client in it counts
            in the cohort size, even if it never appears in ``act``.
        last_m: last observable integer month (right-censoring limit).

    Returns:
        (matrix of % retained by cohort x k with NaN where unobservable,
         cohort sizes, pooled curve weighted by eligible clients).
    """
    cohort_of = cohort_of.astype(int)
    size = cohort_of.value_counts().sort_index()
    x = act.assign(cohort_m=act["cliente_id"].map(cohort_of))
    x = x[x["m"] >= x["cohort_m"]]
    x = x.assign(k=x["m"] - x["cohort_m"])
    active = x.groupby(["cohort_m", "k"])["cliente_id"].nunique().unstack(fill_value=0)
    ks = list(range(0, int(last_m - size.index.min()) + 1))
    matrix = pd.DataFrame(np.nan, index=size.index, columns=ks)
    for c in size.index:
        for k in ks:
            if c + k <= last_m:
                n_active = active.loc[c, k] if (c in active.index and k in active.columns) else 0
                matrix.loc[c, k] = 100 * n_active / size.loc[c]

    curve = []
    for k in ks:
        elig = matrix.index[matrix[k].notna()]
        n_elig = int(size.loc[elig].sum())
        retained = float((matrix.loc[elig, k] / 100 * size.loc[elig]).sum())
        curve.append({"month": k, "eligible_clients": n_elig, "cohorts": len(elig),
                      "retained_clients": int(round(retained)),
                      "retention_pct": 100 * retained / n_elig if n_elig else float("nan")})
    labels = [month_label(c) for c in size.index]
    matrix.index = labels
    size.index = labels
    return matrix, size, pd.DataFrame(curve)


def plot_retention(r: dict) -> str:
    mat = r["matrix"]
    size = r["cohort_size"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5.6), gridspec_kw={"width_ratios": [1.7, 1]})
    vals = mat.values.astype(float)
    masked = np.ma.masked_invalid(vals)
    im = ax1.imshow(masked, cmap=SEQ_BLUES, vmin=50, vmax=100, aspect="auto")
    for i in range(vals.shape[0]):
        for j in range(vals.shape[1]):
            v = vals[i, j]
            if np.isfinite(v):
                ax1.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=7.5,
                         color="white" if v >= 85 else INK)
    ax1.set_xticks(range(vals.shape[1]), [str(c) for c in mat.columns])
    ax1.set_yticks(range(vals.shape[0]), [f"{c}  (n={size.loc[c]})" for c in mat.index])
    ax1.set_xlabel("Months since first approved transaction")
    ax1.grid(False)
    for s in ax1.spines.values():
        s.set_visible(False)
    ax1.set_title("Retention by cohort (% of cohort active in month k)", loc="left", fontsize=10.5, color=INK_2)
    cb = fig.colorbar(im, ax=ax1, fraction=0.03, pad=0.02)
    cb.outline.set_visible(False)
    cb.ax.tick_params(labelsize=8)
    cb.ax.yaxis.set_major_formatter(PercentFormatter(100, decimals=0))

    c = r["curve_reliable"]
    lg = r["legacy_curve"]
    lg = lg[lg["month"] <= c["month"].max()]
    ax2.plot(lg["month"], lg["retention_pct"], color=GREY, lw=1.6, marker="o", ms=3.5,
             label="Phase-1 definition (cohort = signup month)")
    ax2.plot(c["month"], c["retention_pct"], color=BLUE, lw=2.2, marker="o", ms=5,
             label="Corrected (cohort = first transaction)")
    key = c[c["month"].isin([1, 3, 6])]
    ax2.scatter(key["month"], key["retention_pct"], s=70, facecolor="white", edgecolor=BLUE, linewidth=2, zorder=4)
    summary = "\n".join(f"Month {int(m)}: {v:.1f}%" for m, v in zip(key["month"], key["retention_pct"]))
    ax2.text(4.6, 66, "Corrected retention\n" + summary, fontsize=9, color=INK, va="center", linespacing=1.5)
    ax2.set_ylim(30, 104)
    ax2.legend(loc="lower right", fontsize=8.5)
    ax2.annotate("phase-1 month 0 measured\nactivation lag, not churn", (0, lg["retention_pct"].iloc[0]),
                 xytext=(1.3, 50), textcoords="data", fontsize=8.5, color=INK_2, va="center",
                 arrowprops={"arrowstyle": "-", "color": INK_3, "lw": 0.8})
    ax2.set_xlim(-0.3, c["month"].max() + 0.6)
    ax2.set_xticks(c["month"])
    ax2.yaxis.set_major_formatter(PercentFormatter(100, decimals=0))
    ax2.set_xlabel("Months since first approved transaction")
    ax2.set_ylabel("Clients active in month k")
    ax2.set_title(f"All cohorts pooled (>= {MIN_ELIGIBLE_FOR_CURVE} clients observable)", loc="left",
                  fontsize=10.5, color=INK_2)
    headline(fig, f"Clients stay for a quarter, then churn sets in: retention {r['m3']:.0f}% at month 3, "
                  f"{r['m6']:.0f}% at month 6",
             "Cohort = month of first approved transaction (clients who signed up in the window). Signup-month "
             "cohorts mixed activation lag with churn and made retention rise over time.")
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    return save(fig, "07_retencion_cohortes.png", top=0.80)


# --------------------------------------------------------------------------- 6. ticket size
def ticket_stats(d: Data) -> pd.DataFrame:
    """Descriptive statistics of approved ticket size per segment (and overall)."""
    a = d.approved

    def describe(s: pd.Series) -> dict:
        return {"n": int(s.size), "mean": float(s.mean()), "median": float(s.median()),
                "p90": float(s.quantile(0.9)), "p99": float(s.quantile(0.99)),
                "skew": float(s.skew()), "skew_log": float(np.log(s).skew()),
                "mean_over_median": float(s.mean() / s.median())}

    rows = {SEGMENT_LABELS[sg]: describe(a.loc[a["segmento"] == sg, "monto_usd"]) for sg in SEGMENT_ORDER}
    rows["All"] = describe(a["monto_usd"])
    return pd.DataFrame(rows).T


def plot_ticket(d: Data, ts: pd.DataFrame) -> str:
    a = d.approved
    bins = np.logspace(np.log10(1), np.log10(a["monto_usd"].quantile(0.999)), 70)
    fig, axes = plt.subplots(3, 1, figsize=(10, 6.6), sharex=True)
    for ax, sg in zip(axes, SEGMENT_ORDER):
        lbl = SEGMENT_LABELS[sg]
        vals = a.loc[a["segmento"] == sg, "monto_usd"]
        ax.hist(vals, bins=bins, color=SEGMENT_COLORS[sg], alpha=0.9, rwidth=0.9)
        med, mean = ts.loc[lbl, "median"], ts.loc[lbl, "mean"]
        ax.axvline(med, color=INK, lw=1.4)
        ax.axvline(mean, color=INK, lw=1.4, ls=(0, (2, 2)))
        ymax = ax.get_ylim()[1]
        box = {"boxstyle": "square,pad=0.15", "facecolor": "white", "edgecolor": "none", "alpha": 0.9}
        ax.text(med / 1.08, ymax * 0.9, f"median ${med:,.0f}", ha="right", va="top", fontsize=8.5, color=INK,
                bbox=box)
        ax.text(mean * 1.08, ymax * 0.9, f"mean ${mean:,.0f}", ha="left", va="top", fontsize=8.5, color=INK,
                bbox=box)
        ax.text(0.005, 0.9, lbl, transform=ax.transAxes, fontsize=10, fontweight="semibold", color=INK, va="top")
        ax.yaxis.set_major_formatter(FuncFormatter(fmt_thousands))
        ax.set_ylabel("Transactions", fontsize=8.5)
    axes[-1].set_xscale("log")
    axes[-1].xaxis.set_major_formatter(FuncFormatter(fmt_usd))
    axes[-1].set_xlabel("Approved ticket (USD, log scale)  -  solid line: median, dashed line: mean")
    overall = ts.loc["All"]
    headline(fig, f"The mean ticket is {overall['mean_over_median']:.1f}x the median: report the median",
             f"Amounts are log-normal (skew {overall['skew']:.1f} in USD, {overall['skew_log']:.2f} after log): a few "
             "large tickets pull the mean up; the median describes the typical purchase.")
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    return save(fig, "08_distribucion_ticket.png", top=0.84)


# --------------------------------------------------------------------------- 7. anomalies
def spike_scores(approved: pd.DataFrame, min_history: int = MIN_CARD_HISTORY) -> pd.DataFrame:
    """Per-transaction anomaly scores for upward spending spikes, computed per card.

    Scores (higher = more anomalous):
        z_naive   (amount - card mean) / card SD on raw USD      -> classic 3-sigma rule
        z_log     same, on the category-adjusted log amount      -> fixes skew, still non-robust
        iqr_score (r - Q3) / IQR on the category-adjusted log    -> Tukey fence when > 1.5
        z_robust  0.6745 * (r - median) / MAD on the same r      -> modified z (Iglewicz-Hoaglin)
        x_median  amount / card median (raw USD)                 -> phase-1 SQL rule 6.2

    r = ln(amount) - median ln(amount) of the merchant category: a USD 900 software
    bill is normal, a USD 900 meal is not. Only cards with >= `min_history` approved
    transactions are scored (a per-card baseline needs history).
    """
    s = approved.loc[:, ["transaccion_id", "tarjeta_id", "categoria_comercio", "monto_usd", "fecha_hora"]].copy()
    s = s[s.groupby("tarjeta_id")["monto_usd"].transform("size") >= min_history].copy()
    log_amt = np.log(s["monto_usd"])
    s["r"] = log_amt - log_amt.groupby(s["categoria_comercio"]).transform("median")

    # Population SD (ddof=0), the same estimator as sql/06_anomalias.sql, so both report identical counts.
    g_raw = s.groupby("tarjeta_id")["monto_usd"]
    s["z_naive"] = (s["monto_usd"] - g_raw.transform("mean")) / g_raw.transform(lambda x: x.std(ddof=0))
    g = s.groupby("tarjeta_id")["r"]
    s["z_log"] = (s["r"] - g.transform("mean")) / g.transform(lambda x: x.std(ddof=0))
    s["x_median"] = s["monto_usd"] / g_raw.transform("median")  # used by the phase-1 SQL rule 6.2
    q1, q3 = g.transform(lambda x: x.quantile(0.25)), g.transform(lambda x: x.quantile(0.75))
    s["iqr_score"] = (s["r"] - q3) / (q3 - q1)
    med = g.transform("median")
    mad = (s["r"] - med).abs().groupby(s["tarjeta_id"]).transform("median")
    s["z_robust"] = 0.6745 * (s["r"] - med) / mad.replace(0, np.nan)
    return s


@dataclass
class EvalRow:
    """Precision / recall / F1 of one detection method."""

    method: str
    level: str
    flagged: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    flag_rate_pct: float


def evaluate(method: str, level: str, flagged: set, positives: set, population: int) -> EvalRow:
    """Compare a set of flagged ids with the ground-truth set."""
    tp = len(flagged & positives)
    fp = len(flagged - positives)
    fn = len(positives - flagged)
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return EvalRow(method, level, len(flagged), tp, fp, fn, p, r, f1, 100 * len(flagged) / population)


def detect_card_testing(clean: pd.DataFrame, max_usd: float = BURST_MAX_USD, window_min: int = BURST_WINDOW_MIN,
                        min_tx: int = BURST_MIN_TX) -> tuple[set[int], pd.Series]:
    """Burst rule: >= `min_tx` micro-charges (0 < USD < max_usd) on one card within `window_min` minutes.

    Any status counts (declined attempts are the signal). Returns the ids of every
    transaction inside a qualifying window and, per card, the largest number of
    micro-charges observed in any window (used for the separation chart).
    """
    small = clean[(clean["monto_usd"] > 0) & (clean["monto_usd"] < max_usd)]
    small = small.sort_values(["tarjeta_id", "fecha_hora"])
    window = np.timedelta64(window_min, "m")
    flagged: set[int] = set()
    max_in_window: dict[int, int] = {}
    for card, grp in small.groupby("tarjeta_id"):
        t = grp["fecha_hora"].to_numpy()
        ids = grp["transaccion_id"].to_numpy()
        ends = np.searchsorted(t, t + window, side="right")
        counts = ends - np.arange(len(t))
        max_in_window[int(card)] = int(counts.max())
        for i in np.flatnonzero(counts >= min_tx):
            flagged.update(int(x) for x in ids[i:ends[i]])
    return flagged, pd.Series(max_in_window, name="max_micro_tx_10min").sort_values(ascending=False)


def precision_recall_curve(score: pd.Series, y: pd.Series) -> tuple[np.ndarray, np.ndarray, float]:
    """Precision/recall over every threshold, plus average precision (area under the PR curve)."""
    order = np.argsort(-score.fillna(-np.inf).to_numpy(), kind="stable")
    yy = y.to_numpy()[order]
    tp = np.cumsum(yy)
    k = np.arange(1, len(yy) + 1)
    precision, recall = tp / k, tp / yy.sum()
    ap = float(precision[yy].sum() / yy.sum())
    return precision, recall, ap


def anomaly_detection(d: Data, gt: pd.DataFrame) -> dict:
    """Run every detector and evaluate it against the injected ground truth."""
    spike_ids = set(gt.loc[gt["tipo"] == "pico_gasto", "transaccion_id"])
    spike_cards = set(gt.loc[gt["tipo"] == "pico_gasto", "tarjeta_id"])
    test_ids = set(gt.loc[gt["tipo"] == "card_testing", "transaccion_id"])
    test_cards = set(gt.loc[gt["tipo"] == "card_testing", "tarjeta_id"])

    s = spike_scores(d.approved)
    in_scope = set(s["transaccion_id"])
    missing = spike_ids - in_scope
    if missing:
        log.warning("%d injected spikes are outside the scored population", len(missing))
    n_tx, n_cards = len(s), s["tarjeta_id"].nunique()

    rules = {
        f"Naive z-score > {NAIVE_Z_THRESHOLD:g} (raw USD, mean/SD)": s["z_naive"] > NAIVE_Z_THRESHOLD,
        f"z-score > {NAIVE_Z_THRESHOLD:g} on log amount (mean/SD)": s["z_log"] > NAIVE_Z_THRESHOLD,
        f"Tukey IQR fence (Q3 + {IQR_K:g} IQR, log amount)": s["iqr_score"] > IQR_K,
        f"Robust modified z > {ROBUST_Z_THRESHOLD:g} (median/MAD, log amount)": s["z_robust"] > ROBUST_Z_THRESHOLD,
    }
    tx_rows = [evaluate(name, "transaction", set(s.loc[mask, "transaccion_id"]), spike_ids, n_tx)
               for name, mask in rules.items()]

    naive_cards = set(s.loc[s["z_naive"] > NAIVE_Z_THRESHOLD, "tarjeta_id"])
    robust_counts = s[s["z_robust"] > ROBUST_Z_THRESHOLD].groupby("tarjeta_id").size()
    cluster_cards = set(robust_counts[robust_counts >= CLUSTER_MIN_FLAGS].index)
    sql62_counts = s[s["x_median"] >= SQL62_MULTIPLE].groupby("tarjeta_id").size()
    sql62_cards = set(sql62_counts[sql62_counts >= SQL62_MIN_TX].index)
    burst_ids, burst_max = detect_card_testing(d.clean)
    burst_cards = set(d.clean.loc[d.clean["transaccion_id"].isin(burst_ids), "tarjeta_id"])
    n_all_cards = d.clean["tarjeta_id"].nunique()
    card_rows = [
        evaluate("Spikes: card has >= 1 naive 3-sigma flag", "card", naive_cards, spike_cards, n_cards),
        evaluate(f"Spikes: card has >= {CLUSTER_MIN_FLAGS} robust flags", "card", cluster_cards, spike_cards, n_cards),
        evaluate(f"Spikes: >= {SQL62_MIN_TX} tx at >= {SQL62_MULTIPLE:g}x card median (sql/06 6.2)*", "card",
                 sql62_cards, spike_cards, n_cards),
        evaluate(f"Card testing: >= {BURST_MIN_TX} tx < USD {BURST_MAX_USD:g} in {BURST_WINDOW_MIN} min",
                 "card", burst_cards, test_cards, n_all_cards),
        evaluate("Card testing: same burst rule (transactions)", "transaction", burst_ids, test_ids, len(d.clean)),
    ]

    # Masking: how much do the spikes contaminate their own card's baseline?
    is_spike = s["transaccion_id"].isin(spike_ids)
    mask_rows = []
    for card in sorted(spike_cards & set(s["tarjeta_id"])):
        c = s[s["tarjeta_id"] == card]
        clean_part = c[~c["transaccion_id"].isin(spike_ids)]
        sp = c[c["transaccion_id"].isin(spike_ids)]
        z_clean = (sp["monto_usd"] - clean_part["monto_usd"].mean()) / clean_part["monto_usd"].std(ddof=0)
        mask_rows.append({
            "tarjeta_id": int(card), "spikes": int(len(sp)),
            "sd_inflation": float(c["monto_usd"].std(ddof=0) / clean_part["monto_usd"].std(ddof=0)),
            "mean_shift_pct": float(100 * (c["monto_usd"].mean() / clean_part["monto_usd"].mean() - 1)),
            "median_shift_pct": float(100 * (c["monto_usd"].median() / clean_part["monto_usd"].median() - 1)),
            "spikes_caught_naive": int((sp["z_naive"] > NAIVE_Z_THRESHOLD).sum()),
            "spikes_caught_if_baseline_clean": int((z_clean > NAIVE_Z_THRESHOLD).sum()),
            "min_spike_z_naive": float(sp["z_naive"].min()), "min_spike_z_clean": float(z_clean.min()),
        })
    masking = pd.DataFrame(mask_rows)

    curves = {}
    for key, label in (("z_naive", "Naive z (raw USD)"), ("z_log", "z on log amount (mean/SD)"),
                       ("iqr_score", "IQR score (log amount)"), ("z_robust", "Robust modified z (median/MAD)")):
        prec, rec, ap = precision_recall_curve(s[key], is_spike)
        curves[key] = {"label": label, "precision": prec, "recall": rec, "ap": ap}

    return {"scores": s, "tx_eval": tx_rows, "card_eval": card_rows, "masking": masking, "curves": curves,
            "burst_max": burst_max, "burst_cards": burst_cards, "test_cards": test_cards,
            "scope_tx": n_tx, "scope_cards": n_cards, "n_spikes": len(spike_ids),
            "n_spike_cards": len(spike_cards), "n_test_tx": len(test_ids), "n_test_cards": len(test_cards),
            "spike_volume_usd": float(d.approved.loc[d.approved["transaccion_id"].isin(spike_ids), "monto_usd"].sum()),
            "test_approved": int(d.clean[d.clean["transaccion_id"].isin(test_ids)]["estatus"].eq("aprobada").sum())}


def plot_masking(an: dict) -> str:
    m = an["masking"].sort_values("sd_inflation")
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.8))
    y = np.arange(len(m))
    labels = [f"Card {c}" for c in m["tarjeta_id"]]
    ax1.barh(y, m["sd_inflation"], color=ORANGE, height=0.6)
    ax1.axvline(1, color=INK_3, lw=0.8)
    for yi, v in zip(y, m["sd_inflation"]):
        ax1.text(v + 0.05, yi, f"{v:.1f}x", va="center", fontsize=8.5, color=INK)
    ax1.set_yticks(y, labels)
    ax1.set_xlim(0, m["sd_inflation"].max() * 1.15)
    ax1.xaxis.set_major_locator(MultipleLocator(1))
    ax1.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}x"))
    ax1.grid(axis="y", visible=False)
    ax1.grid(axis="x", visible=True)
    ax1.set_title("Standard deviation: with spikes / without", loc="left", fontsize=10.5, color=INK_2)

    h = 0.36
    ax2.barh(y + h / 2, m["mean_shift_pct"], height=h, color=ORANGE, label="Mean")
    ax2.barh(y - h / 2, m["median_shift_pct"], height=h, color=BLUE, label="Median")
    ax2.set_yticks(y, labels)
    ax2.xaxis.set_major_formatter(PercentFormatter(100, decimals=0))
    ax2.grid(axis="y", visible=False)
    ax2.grid(axis="x", visible=True)
    ax2.legend(loc="lower right")
    ax2.set_title("Shift caused by the spikes", loc="left", fontsize=10.5, color=INK_2)
    headline(fig, f"Spikes hide themselves: they inflate their card's SD {m['sd_inflation'].median():.1f}x "
                  f"(median) while the median moves {m['median_shift_pct'].abs().median():.1f}%",
             "Masking: a 3-sigma threshold computed on contaminated data rises with the very outliers it should "
             "catch. The median and MAD barely react.")
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    return save(fig, "09_enmascaramiento.png", top=0.80)


def plot_pr(an: dict) -> str:
    fig, ax = plt.subplots(figsize=(10, 5.4))
    colors = {"z_naive": ORANGE, "z_log": YELLOW, "iqr_score": AQUA, "z_robust": BLUE}
    thresholds = {"z_naive": NAIVE_Z_THRESHOLD, "z_log": NAIVE_Z_THRESHOLD, "iqr_score": IQR_K,
                  "z_robust": ROBUST_Z_THRESHOLD}
    s = an["scores"]
    for key, c in an["curves"].items():
        n_show = int(np.searchsorted(c["recall"], 1.0)) + 1  # stop once every spike has been found
        ax.plot(c["recall"][:n_show], c["precision"][:n_show], color=colors[key], lw=2,
                label=f"{c['label']}  (AP {c['ap']:.2f})")
        k = int((s[key] > thresholds[key]).sum())  # operating point at the standard threshold
        if k:
            ax.scatter([c["recall"][k - 1]], [c["precision"][k - 1]], s=55, color=colors[key], zorder=4,
                       edgecolor="white", linewidth=1.5)
    ax.set_xlim(0, 1.02)
    ax.set_ylim(0, 1.02)
    ax.xaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax.set_xlabel("Recall (share of the 32 injected spikes found)")
    ax.set_ylabel("Precision (share of flags that are real spikes)")
    ax.grid(axis="x", visible=True)
    ax.legend(loc="upper right", title="Score  (dots = standard threshold)", title_fontsize=8.5)
    rob = next(r for r in an["tx_eval"] if r.method.startswith("Robust"))
    nai = next(r for r in an["tx_eval"] if r.method.startswith("Naive"))
    headline(fig, f"Robust statistics cut false alarms {nai.false_positives / max(rob.false_positives, 1):.0f}x: "
                  f"{rob.precision:.0%} precision vs {nai.precision:.1%} for the 3-sigma rule",
             f"Precision-recall over all thresholds, {an['scope_tx']:,} approved transactions on cards with >= "
             f"{MIN_CARD_HISTORY} transactions. AP = average precision (1.00 is perfect).")
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    return save(fig, "10_precision_recall.png", top=0.82)


def plot_card_testing(an: dict) -> str:
    bm = an["burst_max"]
    test_cards = an["test_cards"]
    fig, ax = plt.subplots(figsize=(10, 4.8))
    others = bm[~bm.index.isin(test_cards)]
    tests = bm[bm.index.isin(test_cards)]
    xs = np.arange(1, bm.max() + 1)
    other_counts = others.value_counts().reindex(xs, fill_value=0)
    test_counts = tests.value_counts().reindex(xs, fill_value=0)
    ax.bar(xs, other_counts.values, color=GREY, width=0.72, label="Other cards with micro-charges")
    ax.bar(xs, test_counts.values, bottom=other_counts.values, color=ORANGE, width=0.72,
           label="Injected card-testing cards")
    # Log scale: one bar holds hundreds of cards, the others hold one to three.
    ax.set_yscale("log")
    top = max(other_counts.max(), test_counts.max()) * 6
    ax.set_ylim(0.7, top)
    for x in other_counts[other_counts > 0].index:
        ax.text(x, other_counts.loc[x] * 1.2, f"{other_counts.loc[x]:,} cards", ha="center", va="bottom",
                fontsize=8.5, color=INK)
    for x in test_counts[test_counts > 0].index:
        ids = ", ".join(str(c) for c in tests[tests == x].index)
        label = f"card{'s' if test_counts.loc[x] > 1 else ''}\n{ids}"
        ax.text(x, (other_counts.loc[x] + test_counts.loc[x]) * 1.2, label, ha="center", va="bottom",
                fontsize=8, color=INK)
    ax.axvline(BURST_MIN_TX - 0.5, color=INK_3, lw=1)
    ax.text(BURST_MIN_TX - 0.35, top * 0.55, f"alert threshold: >= {BURST_MIN_TX}", fontsize=9, color=INK_2,
            va="top")
    ax.set_xticks(xs)
    ax.set_xlim(0.3, bm.max() + 0.7)
    ax.yaxis.set_major_formatter(FuncFormatter(fmt_thousands))
    ax.set_xlabel(f"Largest number of micro-charges (< USD {BURST_MAX_USD:g}) in any {BURST_WINDOW_MIN}-minute window")
    ax.set_ylabel("Cards (log scale)")
    ax.legend(loc="upper right", bbox_to_anchor=(1, 0.8))
    max_other = int(others.max()) if len(others) else 0
    headline(fig, f"Card testing is unmistakable: {len(tests)} cards fired {int(tests.min())}-{int(tests.max())} "
                  f"micro-charges within {BURST_WINDOW_MIN} minutes",
             f"Every other card with micro-charges had at most {max_other} in any {BURST_WINDOW_MIN}-minute window, "
             "so a simple velocity rule separates the two groups perfectly.")
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    return save(fig, "11_card_testing.png", top=0.82)


# --------------------------------------------------------------------------- outputs
def eval_table_md(rows: list[EvalRow]) -> str:
    """Markdown table for a list of evaluation rows."""
    out = ["| Method | Level | Flagged | TP | FP | FN | Precision | Recall | F1 |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        out.append(f"| {r.method} | {r.level} | {r.flagged:,} | {r.true_positives} | {r.false_positives:,} | "
                   f"{r.false_negatives} | {r.precision:.1%} | {r.recall:.1%} | {r.f1:.3f} |")
    return "\n".join(out)


def write_findings(t: dict, rp: dict, s: dict, mx: dict, p: dict, r: dict, ts: pd.DataFrame, an: dict,
                   figs: dict[str, str], dq: dict) -> Path:
    """Compose reports/hallazgos.md from the computed results (no hard-coded numbers)."""
    seg = mx["segments"]
    corp, st = seg.loc["Corporativo"], seg.loc["Startup"]
    act = r["activation"]
    nai = next(x for x in an["tx_eval"] if x.method.startswith("Naive"))
    rob = next(x for x in an["tx_eval"] if x.method.startswith("Robust"))
    zlog = next(x for x in an["tx_eval"] if x.method.startswith("z-score"))
    iqr = next(x for x in an["tx_eval"] if x.method.startswith("Tukey"))
    naive_card, cluster_card, sql62_card, burst_card, burst_tx = an["card_eval"]
    msk = an["masking"]
    allts = ts.loc["All"]
    fp_ratio = nai.false_positives / max(rob.false_positives, 1)
    legacy = r["legacy"]
    spc_change = 100 * (t["spend_per_client_growth"] - 1)
    all_from_new = t["share_growth_from_clients_pct"] >= 100

    def month_fmt(m: str) -> str:
        return pd.Timestamp(m).strftime("%b %Y")

    def fig(key: str, alt: str) -> str:
        return f"![{alt}]({figs[key]})"

    growth_source = ("all of it came from new clients" if all_from_new
                     else f"about {t['share_growth_from_clients_pct']:.0f}% of it came from new clients")
    md = f"""# Corporate card payments: key findings

_Synthetic dataset, {month_fmt(t['first_month'])} to {month_fmt(t['last_month'])}: {dq['raw_transactions']:,} raw
transactions from {len(mx['segments'])} client segments in Mexico, Colombia and Brazil. Every number below is computed
by `python/analisis.py` on the cleaned view (`sql/01_limpieza.sql`). Spend means approved transactions in USD at fixed
FX rates. Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')}._

## Executive summary

### 1. Volume grew {t['growth_multiple']:.1f}x in twelve months, and {growth_source}

Approved volume went from {fmt_usd(t['first_volume'])} in {month_fmt(t['first_month'])} to
{fmt_usd(t['last_volume'])} in {month_fmt(t['last_month'])}, a compound {t['cmgr_pct']:.1f}% per month; only
{t['negative_mom_months']} of 11 months declined. Active clients grew x{t['clients_growth']:.2f} (from
{t['first_clients']} to {t['last_clients']}), while spend per active client changed {spc_change:+.0f}%.

- **Why:** existing clients are not spending less. Legacy clients (signed up before the window) changed
  x{rp['legacy_spend_growth']:.2f}. The average falls because new clients ramp up slowly: in their first full month
  they spend {rp['m1_pct_of_steady']:.0f}% of their steady-state level, because they start with
  {rp['m0_cards']:.1f} active cards and reach {rp['steady_mean_cards']:.1f} only by month 3. In
  {month_fmt(t['last_month'])}, {rp['pct_active_in_ramp_last_month']:.0f}% of active clients were still in their first
  three months.
- **Action:** keep funding acquisition, and shorten the ramp: issue all planned cards during onboarding and run a
  90-day activation playbook. Report spend per client separately for ramping and mature clients, so that fast growth
  does not look like declining engagement.

{fig('trend', 'Monthly volume and growth drivers')}

{fig('ramp', 'Ramp-up of new clients')}

### 2. Corporate clients are {corp['client_share']:.0%} of the base but {corp['spend_share']:.0%} of spend

- **Why:** the drivers multiply. Compared with a Startup, a Corporate client uses
  {corp['cards_per_client'] / st['cards_per_client']:.1f}x as many cards, each card transacts
  {corp['tx_per_card'] / st['tx_per_card']:.1f}x as often, and the mean ticket is
  {corp['mean_ticket'] / st['mean_ticket']:.1f}x larger. Spend per client is therefore
  {corp['spend_per_client'] / st['spend_per_client']:.0f}x higher ({fmt_usd(corp['spend_per_client'])} vs
  {fmt_usd(st['spend_per_client'])} over the period). Travel is the largest category
  ({mx['category_share'].get('Viajes', 0):.0%} of all spend) and the top category within Corporate spend
  ({mx['corporate_category_share'].get('Viajes', 0):.0%}).
- **Action:** prioritise Corporate acquisition and card-issuance campaigns. For Startups and SMEs the cheapest lever
  is cards per client, because spend scales with every extra active card.

{fig('segments', 'Spend by segment')}

{fig('catcty', 'Spend by category and country')}

### 3. Spend is concentrated: {p['clients_needed'][80]} clients ({p['pct_clients_needed'][80]:.0f}%) generate 80% of it

The top 10 clients alone account for {p['top10_share']:.0%} of spend (Gini {p['gini']:.2f}). The 80% group is
{p['top80_segment_mix']['Corporativo']:.0%} Corporate.

- **Why:** the segment economics above, amplified by the wide (log-normal) spread of activity between clients.
- **Action:** run a key-account program (a named owner and a quarterly business review) for the 80% group. Add a
  concentration-risk alert when any single client exceeds a set share of monthly volume.

{fig('pareto', 'Pareto curve of clients')}

### 4. New clients take a median of {act['median_days_to_first_tx']:.0f} days to make their first purchase

Only {act['pct_first_tx_in_signup_month']:.0f}% of the {act['new_clients']} clients who signed up in the window
transacted in their signup month. The median time from signup to first approved transaction is
{act['median_days_to_first_tx']:.0f} days (75th percentile {act['p75_days_to_first_tx']:.0f} days).

- **Why:** nothing can be spent before a card exists, and the first card is issued a median of
  {act['median_days_signup_to_first_card']:.0f} days after signup.
- **Action:** issue a virtual card at signup and track time to first transaction as an onboarding KPI. This lag is also
  what broke the phase-1 retention metric (see Methods).

### 5. Active clients stay for a quarter, then churn sets in

Retention is the share of a cohort that is active in month k after its first transaction. It is {r['m1']:.1f}% at
month 1, {r['m3']:.1f}% at month 3 and {r['m6']:.1f}% at month 6. The last month with at least
{MIN_ELIGIBLE_FOR_CURVE} observable clients is month {r['last_reliable_month']}, at {r['last_reliable_pct']:.1f}%.

- **Why:** usage is sticky early, because cards get embedded into expense workflows; the loss concentrates after the
  first quarter ({r['m3'] - r['m6']:.1f} points lost between months 3 and 6, versus {100 - r['m3']:.1f} in months 0-3).
  Disengagement is also visible among clients who stay: active cards per client slip from
  {rp['peak_cards']:.1f} at month {rp['peak_cards_month']} to {rp['last_ramp_cards']:.1f} at month
  {rp['last_ramp_month']}.
- **Action:** build a health score on month-over-month changes in spend and active cards, and have customer success
  reach out between months 3 and 6, before clients go silent.

{fig('retention', 'Cohort retention')}

### 6. Month-end closing drives spend: days 26-31 run {100 * (s['end_vs_mid'] - 1):.0f}% above a normal weekday

Days 26-31 hold {s['share_tx_days_26_31']:.1%} of approved transactions but are only
{s['calendar_share_days_26_31']:.1%} of calendar days. Days 1-3 run {100 * (s['start_vs_mid'] - 1):+.0f}% and the 15th
runs {100 * (s['day15_vs_mid'] - 1):+.0f}%. Weekends carry {s['weekend_vs_weekday_tx']:.0%} of a weekday's transactions.

- **Why:** companies settle vendors, subscriptions and expense reports around the monthly close.
- **Action:** plan authorisation capacity, fraud-review staffing and credit-limit headroom for the last week of the
  month. Compare month-end metrics with other month-ends, not with mid-month.

{fig('season', 'End-of-month seasonality')}

### 7. The typical purchase is {fmt_usd(allts['median'])}, not {fmt_usd(allts['mean'])}

Ticket sizes are log-normal: skew is {allts['skew']:.1f}, and {allts['skew_log']:.2f} after a log transform. A long right
tail pulls the mean up to {allts['mean_over_median']:.1f}x the median (p90 {fmt_usd(allts['p90'])}).

- **Why it matters:** a mean-based "average ticket" overstates what most clients spend, and it moves with a handful of
  large invoices.
- **Action:** report the median (and p90) for ticket KPIs. Keep the mean only where totals matter
  (revenue = count x mean).

{fig('ticket', 'Ticket size distribution')}

### 8. Robust anomaly rules raise {fp_ratio:.0f}x fewer false alarms than the 3-sigma rule

The classic per-card 3-sigma rule was run over {an['scope_tx']:,} approved transactions. It raises {nai.flagged:,}
alerts ({nai.flag_rate_pct:.1f}% of transactions) to find {nai.true_positives} of the {an['n_spikes']} injected spending
spikes, a precision of {nai.precision:.1%}. A robust modified z-score (median/MAD on category-adjusted log amounts)
raises {rob.flagged:,} alerts with {rob.precision:.0%} precision and {rob.recall:.0%} recall. Requiring
{CLUSTER_MIN_FLAGS} or more robust alerts on the same card isolates {cluster_card.true_positives} of the
{an['n_spike_cards']} compromised cards among {cluster_card.flagged} flagged cards; the 3-sigma rule flags
{naive_card.flagged} cards. A velocity rule catches all {burst_card.true_positives} card-testing cards with
{burst_card.false_positives} false positives.

- **Why:** amounts are skewed, so a value 3 standard deviations above the mean is routine for a legitimate large bill.
  The spikes also inflate the very standard deviation used to detect them (masking).
- **Action:** replace the 3-sigma alert with the robust score plus the burst rule, and queue investigations by card.
  Card-testing bursts should trigger an automatic block, because {an['test_approved']} of {an['n_test_tx']} test
  charges were approved.

{fig('pr', 'Precision-recall of anomaly detectors')}

## Methods

**Data and cleaning.** The raw table has {dq['raw_transactions']:,} rows. The view `transacciones_limpias` removes
duplicates, NULL merchants, negative amounts that are not reversals, and future-dated rows, which leaves
{dq['clean_transactions']:,} rows ({dq['removed_pct']:.2f}% removed; see `reports/calidad_datos.md`). Reversals are
negative by design and are excluded from spend. Spend uses approved transactions only, the same definition as
`sql/02_metricas_mensuales.sql`. The {an['n_spikes']} injected spikes add {fmt_usd(an['spike_volume_usd'])}
({100 * an['spike_volume_usd'] / t['total_volume']:.2f}% of total volume), so they do not change the business findings.

**Growth decomposition.** Monthly volume = active clients x spend per active client, so
ln(V_last / V_first) = ln(clients ratio) + ln(spend-per-client ratio). Client growth accounts for
{t['share_growth_from_clients_pct']:.0f}% of the log growth; a value above 100% means spend per client fell. The ramp
curve uses new clients only. Tenure is measured in months since the first approved transaction, steady state is the
median of months 3 and later, and month 0 is usually a partial month.

**Seasonality.** Only weekdays are used, because the weekend effect is separate. Each day is divided by the average
weekday of its own month, which cancels the growth trend. The ratios are then averaged by day of month and re-based so
that a mid-month weekday (days 4-25, excluding the 15th) = 1.00.

**Segment decomposition.** Spend per active client = cards per client x approved transactions per card x mean ticket.
The mean is the correct statistic for this identity, because totals are count x mean.

**Cohorts and retention (fixed in phase 2).** Phase 1 used *cohort = signup month*. Recomputed here, that definition
gives {legacy['m1']:.1f}% at month 1, {legacy['m3']:.1f}% at month 3 and {legacy['m6']:.1f}% at month 6. Retention that
rises between month 1 and month 3 is a red flag. The first card arrives a median of
{act['median_days_signup_to_first_card']:.0f} days after signup, so many clients had not transacted yet in their signup
month (only {legacy['m0']:.1f}% were "active" in month 0). Those early columns measured *activation*, not retention.

The corrected definition:

- cohort = the month of the client's first approved transaction;
- only clients who signed up inside the window are included, because for older clients the true first month cannot
  be observed;
- retention(k) = the share of the cohort with at least one approved transaction in month k;
- a cohort contributes only to months that are observable, and the pooled curve is weighted by eligible clients.

`sql/05_cohortes_retencion.sql` was rewritten with the same definition and returns the same numbers. Points with fewer
than {MIN_ELIGIBLE_FOR_CURVE} eligible clients are left out of the pooled curve, because late months come from only one
or two small cohorts.

**Why the median beats the mean here.** If ln(amount) ~ N(mu, sigma^2), the median is e^mu but the mean is
e^(mu + sigma^2/2). The mean sits above the typical value by a factor that grows with the spread, and a few large
tickets move it. The median is not affected by the tail.

| Segment | Approved tx | Mean | Median | p90 | p99 | Skew | Skew of log | Mean / median |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
""" + "\n".join(
        f"| {idx} | {row['n']:,.0f} | ${row['mean']:,.0f} | ${row['median']:,.0f} | ${row['p90']:,.0f} | "
        f"${row['p99']:,.0f} | {row['skew']:.1f} | {row['skew_log']:.2f} | {row['mean_over_median']:.2f}x |"
        for idx, row in ts.iterrows()) + f"""

**Anomaly detection.** The ground truth is `data/anomalias_inyectadas.csv`: {an['n_spikes']} spike transactions on
{an['n_spike_cards']} cards (amounts set to 10-30x the card's mean) and {an['n_test_tx']} card-testing transactions on
{an['n_test_cards']} cards. Spike detectors score every approved transaction on cards with at least
{MIN_CARD_HISTORY} approved transactions ({an['scope_tx']:,} transactions on {an['scope_cards']} cards; all
{an['n_spikes']} spikes are in scope). Each transaction is scored against its own card's history:

- *Naive z-score*: (amount - card mean) / card SD on raw USD; alert if > {NAIVE_Z_THRESHOLD:g}.
- *z-score on log amount*: the same formula on r = ln(amount) - median ln(amount) of the merchant category. This fixes
  the skew (and a USD 900 meal is no longer compared with a USD 900 software bill), but it still uses mean and SD.
- *Tukey IQR fence*: alert if r > Q3 + {IQR_K:g} x IQR of the card.
- *Robust modified z*: 0.6745 x (r - median) / MAD; alert if > {ROBUST_Z_THRESHOLD:g} (Iglewicz and Hoaglin).
- *Card-level cluster rule*: alert on a card with at least {CLUSTER_MIN_FLAGS} robust alerts.
- *Card-testing burst rule*: alert when at least {BURST_MIN_TX} transactions below USD {BURST_MAX_USD:g} (any status)
  hit the same card within {BURST_WINDOW_MIN} minutes.

All thresholds are the textbook defaults; none were tuned on the ground truth. The precision-recall chart shows every
threshold.

## Anomaly-detection evaluation

**Spending spikes, transaction level** ({an['n_spikes']} positives among {an['scope_tx']:,} transactions)

{eval_table_md(an['tx_eval'])}

**Card level and card testing**

{eval_table_md(an['card_eval'])}

**Reading the tables**

- **The 3-sigma rule produces too many false positives.** On log-normal data a z above 3 is not rare, so
  {nai.false_positives:,} legitimate transactions on {naive_card.flagged} of {an['scope_cards']} cards are flagged.
  An analyst would review {nai.flagged / max(nai.true_positives, 1):.0f} alerts for every real spike.
- **The 3-sigma rule also suffers from masking.** The spikes inflate their card's SD by
  {msk['sd_inflation'].min():.1f}-{msk['sd_inflation'].max():.1f}x (median {msk['sd_inflation'].median():.1f}x), while
  the median moves only {msk['median_shift_pct'].abs().median():.1f}% (median across cards). With the contaminated
  baseline, 3-sigma catches {int(msk['spikes_caught_naive'].sum())} spikes; with a clean baseline the same rule would
  catch {int(msk['spikes_caught_if_baseline_clean'].sum())}. Switching to log amounts alone removes most false
  positives ({zlog.flagged:,} alerts, {zlog.precision:.0%} precision), but mean and SD are still contaminated, and over
  all thresholds that score is dominated by the robust one (average precision {an['curves']['z_log']['ap']:.2f} vs
  {an['curves']['z_robust']['ap']:.2f}). The fix has to be robust statistics, not only a transformation.
- **Robust statistics hold the threshold in place.** The median and MAD barely move when outliers are present, so the
  threshold stays where it should. The modified z reaches {rob.precision:.0%} precision at {rob.recall:.0%} recall
  (F1 {rob.f1:.2f} vs {nai.f1:.3f}). The IQR fence trades precision for recall ({iqr.precision:.0%} / {iqr.recall:.0%}).
  Average precision over all thresholds is {an['curves']['z_robust']['ap']:.2f} for the robust z and
  {an['curves']['z_naive']['ap']:.2f} for the naive z.
- **Spikes are a card-level pattern.** Two or more robust alerts on the same card is a strong signal
  ({cluster_card.precision:.0%} precision, {cluster_card.recall:.0%} of compromised cards found). The card is also the
  natural unit for an investigation queue.
- **(\\*) Phase-1 SQL rule.** This rule (at least {SQL62_MIN_TX} transactions at {SQL62_MULTIPLE:g}x or more the card's median)
  flags {sql62_card.flagged} cards and finds {sql62_card.true_positives} of {an['n_spike_cards']}. It is also
  median-based, which is why it works, but its thresholds were set while building the dataset with knowledge of the
  injected 10-30x multipliers, so read it as an optimistic upper bound rather than a fair benchmark.
- **Card testing needs a velocity rule, not an amount rule.** The charges are tiny, so amount-based scores never see
  them. The burst rule finds {burst_tx.true_positives} of {an['n_test_tx']} test transactions with
  {burst_tx.false_positives} false positives.
- **Limits.** A single amount score per transaction cannot be both precise and complete here. Amounts within a card
  legitimately vary by more than an order of magnitude, so some spikes look like a large normal invoice. Production
  systems add context such as merchant novelty, time of day, velocity and device.

{fig('masking', 'Masking effect of spikes on mean and SD')}

{fig('testing', 'Card-testing bursts')}
"""
    path = REPORTS_DIR / "hallazgos.md"
    path.write_text(md, encoding="utf-8")
    log.info("report written: %s", path.relative_to(REPORTS_DIR.parent))
    return path


def write_metrics(t: dict, rp: dict, s: dict, mx: dict, p: dict, r: dict, ts: pd.DataFrame, an: dict,
                  dq: dict) -> Path:
    """Write the headline numbers as JSON for the dashboard/README."""
    seg = mx["segments"]

    def rnd(x: float, n: int = 2) -> float:
        return round(float(x), n)

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period": {"start": str(WINDOW_START.date()), "end": str(WINDOW_END.date())},
        "data_quality": dq,
        "volume": {
            "total_approved_usd": rnd(t["total_volume"], 0), "approved_transactions": t["total_approved_tx"],
            "first_month": t["first_month"], "last_month": t["last_month"],
            "first_month_usd": rnd(t["first_volume"], 0), "last_month_usd": rnd(t["last_volume"], 0),
            "growth_multiple": rnd(t["growth_multiple"]), "compound_monthly_growth_pct": rnd(t["cmgr_pct"]),
            "avg_mom_growth_pct": rnd(t["avg_mom_pct"]),
            "active_clients_first_month": t["first_clients"], "active_clients_last_month": t["last_clients"],
            "active_clients_growth_multiple": rnd(t["clients_growth"]),
            "spend_per_active_client_change_pct": rnd(100 * (t["spend_per_client_growth"] - 1), 1),
            "share_of_log_growth_from_clients_pct": rnd(t["share_growth_from_clients_pct"], 1),
            "monthly": [{"month": row.mes, "volume_usd": rnd(row.volume_usd, 0), "transactions": int(row.tx),
                         "approved_transactions": int(row.approved_tx), "active_clients": int(row.active_clients),
                         "mom_growth_pct": None if pd.isna(row.mom_pct) else rnd(row.mom_pct)}
                        for row in t["monthly"].itertuples()],
        },
        "client_ramp": {
            "first_full_month_pct_of_steady_spend": rnd(rp["m1_pct_of_steady"], 1),
            "month2_pct_of_steady_spend": rnd(rp["m2_pct_of_steady"], 1),
            "active_cards_month0": rnd(rp["m0_cards"]), "active_cards_steady": rnd(rp["steady_mean_cards"]),
            "steady_median_monthly_spend_usd": rnd(rp["steady_median_spend"], 0),
            "legacy_clients_spend_per_client_change": rnd(rp["legacy_spend_growth"], 3),
            "pct_active_clients_in_first_3_months_last_month": rnd(rp["pct_active_in_ramp_last_month"], 1),
        },
        "seasonality": {"days_26_31_vs_mid_month_weekday": rnd(s["end_vs_mid"], 3),
                        "days_1_3_vs_mid_month_weekday": rnd(s["start_vs_mid"], 3),
                        "day_15_vs_mid_month_weekday": rnd(s["day15_vs_mid"], 3),
                        "share_tx_days_26_31_pct": rnd(100 * s["share_tx_days_26_31"]),
                        "calendar_share_days_26_31_pct": rnd(100 * s["calendar_share_days_26_31"]),
                        "weekend_vs_weekday_tx": rnd(s["weekend_vs_weekday_tx"], 3)},
        "segments": [{"segment": sg, "label": SEGMENT_LABELS[sg], "clients": int(row.clients),
                      "client_share_pct": rnd(100 * row.client_share), "spend_usd": rnd(row.spend, 0),
                      "spend_share_pct": rnd(100 * row.spend_share), "cards_per_client": rnd(row.cards_per_client),
                      "tx_per_card": rnd(row.tx_per_card, 1), "mean_ticket_usd": rnd(row.mean_ticket),
                      "spend_per_client_usd": rnd(row.spend_per_client, 0)}
                     for sg, row in seg.iterrows()],
        "country_spend_share_pct": {k: rnd(100 * v) for k, v in mx["country_share"].items()},
        "category_spend_share_pct": {k: rnd(100 * v) for k, v in mx["category_share"].items()},
        "pareto": {"clients_with_spend": p["clients_with_spend"],
                   "clients_for_50pct": p["clients_needed"][50], "clients_for_80pct": p["clients_needed"][80],
                   "clients_for_90pct": p["clients_needed"][90],
                   "pct_clients_for_80pct": rnd(p["pct_clients_needed"][80], 1),
                   "top10_share_pct": rnd(100 * p["top10_share"], 1), "gini": rnd(p["gini"], 3)},
        "activation": {k: (rnd(v, 1) if isinstance(v, float) else v) for k, v in r["activation"].items()},
        "retention": {
            "definition": "cohort = month of first approved transaction (clients signed up in window); "
                          "retention_k = share of cohort with >= 1 approved transaction in month k",
            "month_1_pct": rnd(r["m1"], 1), "month_3_pct": rnd(r["m3"], 1), "month_6_pct": rnd(r["m6"], 1),
            "phase1_signup_cohort_definition": {k: rnd(v, 1) for k, v in r["legacy"].items()},
            "curve": [{"month": int(row.month), "eligible_clients": int(row.eligible_clients),
                       "retention_pct": rnd(row.retention_pct, 1)} for row in r["curve"].itertuples()],
            "min_eligible_clients_for_reliable_point": MIN_ELIGIBLE_FOR_CURVE,
        },
        "ticket_size_usd": {idx: {k: (int(v) if k == "n" else rnd(v)) for k, v in row.items()}
                            for idx, row in ts.iterrows()},
        "anomaly_detection": {
            "scope": {"transactions": an["scope_tx"], "cards": an["scope_cards"],
                      "injected_spikes": an["n_spikes"], "injected_spike_cards": an["n_spike_cards"],
                      "injected_card_testing_tx": an["n_test_tx"], "injected_card_testing_cards": an["n_test_cards"]},
            "evaluation": [{k: (rnd(v, 4) if isinstance(v, float) else v) for k, v in asdict(e).items()}
                           for e in an["tx_eval"] + an["card_eval"]],
            "average_precision": {k: rnd(c["ap"], 4) for k, c in an["curves"].items()},
            "masking_median_sd_inflation": rnd(an["masking"]["sd_inflation"].median()),
            "card_testing_cards_detected": sorted(int(c) for c in an["burst_cards"]),
        },
    }
    path = REPORTS_DIR / "metricas.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("metrics written: %s", path.relative_to(REPORTS_DIR.parent))
    return path


def data_quality_summary(con: sqlite3.Connection) -> dict:
    """Raw vs clean row counts (the full check suite lives in calidad_datos.py)."""
    raw = int(con.execute("SELECT COUNT(*) FROM transacciones").fetchone()[0])
    clean = int(con.execute("SELECT COUNT(*) FROM transacciones_limpias").fetchone()[0])
    return {"raw_transactions": raw, "clean_transactions": clean, "removed_transactions": raw - clean,
            "removed_pct": round(100 * (raw - clean) / raw, 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH, help="path to the SQLite database")
    args = parser.parse_args(argv)
    setup_logging()
    setup_style()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    for old in FIGURES_DIR.glob("*.png"):  # this script owns reports/figuras: drop stale charts
        old.unlink()

    con = connect(args.db)
    try:
        d = load_data(con)
        dq = data_quality_summary(con)
    finally:
        con.close()
    gt = load_ground_truth()
    log.info("loaded %s clean / %s approved transactions", f"{len(d.clean):,}", f"{len(d.approved):,}")

    t = monthly_trend(d)
    rp = client_ramp(d)
    s = month_end_seasonality(d)
    mx = spend_mix(d)
    p = pareto(d)
    r = activation_and_retention(d)
    ts = ticket_stats(d)
    an = anomaly_detection(d, gt)

    figs = {
        "trend": plot_trend(t), "ramp": plot_ramp(rp), "season": plot_seasonality(s),
        "segments": plot_segments(mx), "catcty": plot_category_country(mx), "pareto": plot_pareto(p),
        "retention": plot_retention(r), "ticket": plot_ticket(d, ts), "masking": plot_masking(an),
        "pr": plot_pr(an), "testing": plot_card_testing(an),
    }
    write_findings(t, rp, s, mx, p, r, ts, an, figs, dq)
    write_metrics(t, rp, s, mx, p, r, ts, an, dq)

    rob = next(x for x in an["tx_eval"] if x.method.startswith("Robust"))
    nai = next(x for x in an["tx_eval"] if x.method.startswith("Naive"))
    log.info("volume x%.2f | retention M1 %.1f%% M3 %.1f%% M6 %.1f%% | spikes F1 naive %.3f vs robust %.3f",
             t["growth_multiple"], r["m1"], r["m3"], r["m6"], nai.f1, rob.f1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
