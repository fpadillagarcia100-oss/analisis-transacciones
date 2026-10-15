"""Tests for the data-quality checks and the anomaly/retention analysis.

Two kinds of tests:
  * unit tests on a tiny in-memory database (fast, no generated data needed);
  * integration tests against data/transacciones.db, checked against the injected
    ground truth in data/anomalias_inyectadas.csv.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest

from calidad_datos import build_checks, run_checks, summarize
from comun import SQL_DIR
from cargar_db import SCHEMA


# --------------------------------------------------------------------------- unit tests
@pytest.fixture()
def tiny_db() -> sqlite3.Connection:
    """In-memory database with the real schema and a handful of hand-made rows."""
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA)
    con.execute("INSERT INTO clientes VALUES (1, 'Acme S.A.S.', 'Colombia', 'PyME', '2026-01-10')")
    con.execute("INSERT INTO tarjetas VALUES (1, 1, 'virtual', 'activa', '2026-01-15')")
    con.execute("INSERT INTO tarjetas VALUES (2, 1, 'fisica', 'activa', '2026-01-15')")  # never used
    rows = [
        (1, 1, 1, "2026-02-01 10:00:00", 41000.0, "COP", "Software", "AWS", "Estados Unidos", "aprobada"),
        (2, 1, 1, "2026-02-01 10:00:00", 41000.0, "COP", "Software", "AWS", "Estados Unidos", "aprobada"),  # dup
        (3, 1, 1, "2026-02-02 11:00:00", -5000.0, "COP", "Oficina", "Kalunga", "Colombia", "aprobada"),  # bad neg
        (4, 1, 1, "2026-02-03 12:00:00", -5000.0, "COP", "Oficina", "Kalunga", "Colombia", "reversada"),  # ok neg
        (5, 1, 1, "2026-12-24 09:00:00", 9000.0, "COP", "Otros", "Tienda", "Colombia", "aprobada"),  # future
        (6, 1, 1, "2026-01-05 09:00:00", 9000.0, "COP", "Otros", None, "Colombia", "rechazada"),  # null + early
    ]
    con.executemany("INSERT INTO transacciones VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return con


def test_checks_count_exact_rows_on_tiny_db(tiny_db: sqlite3.Connection) -> None:
    results = {r.name: r for r in run_checks(tiny_db)}
    assert results["duplicate_transactions"].affected_rows == 1
    assert results["negative_amount_not_reversal"].affected_rows == 1   # the reversal is allowed
    assert results["future_timestamp"].affected_rows == 1
    assert results["null_merchant"].affected_rows == 1
    assert results["transaction_before_card_issue"].affected_rows == 1
    assert results["transaction_before_client_signup"].affected_rows == 1
    assert results["cards_without_transactions"].affected_rows == 1
    assert results["orphan_transaction_card"].affected_rows == 0
    assert results["cards_without_transactions"].population_rows == 2  # % is over cards, not transactions


def test_threshold_logic_and_summary(tiny_db: sqlite3.Connection) -> None:
    results = run_checks(tiny_db)
    for r in results:
        assert r.passed == (r.pct <= r.threshold_pct)
    summary = summarize(results, raw=6, clean=3)
    assert summary["critical_failed"] >= 3  # duplicates, bad negative, future date
    assert summary["checks_total"] == len(build_checks())


# --------------------------------------------------------------------------- integration: data quality
@pytest.mark.parametrize(("check", "injected_type"), [
    ("duplicate_transactions", "duplicado"),
    ("null_merchant", "comercio_nulo"),
    ("negative_amount_not_reversal", "monto_negativo"),
    ("future_timestamp", "fecha_futura"),
])
def test_quality_checks_find_every_injected_issue(con, ground_truth, check, injected_type) -> None:
    results = {r.name: r for r in run_checks(con)}
    expected = int((ground_truth["tipo"] == injected_type).sum())
    assert results[check].affected_rows == expected


def test_referential_integrity_and_domains_pass(con) -> None:
    results = {r.name: r for r in run_checks(con)}
    for name in ("orphan_transaction_card", "orphan_transaction_client", "orphan_card_client",
                 "client_card_mismatch", "currency_domain", "category_domain", "status_domain"):
        assert results[name].passed, name


# --------------------------------------------------------------------------- integration: anomalies
def test_burst_rule_finds_exactly_the_card_testing_cards(data, ground_truth) -> None:
    from analisis import detect_card_testing

    flagged_ids, _ = detect_card_testing(data.clean)
    flagged_cards = set(data.clean.loc[data.clean["transaccion_id"].isin(flagged_ids), "tarjeta_id"])
    truth = ground_truth[ground_truth["tipo"] == "card_testing"]
    assert flagged_cards == set(truth["tarjeta_id"])
    assert len(flagged_cards) == 5
    assert flagged_ids == set(truth["transaccion_id"])  # every test charge, and nothing else


def test_robust_method_beats_naive_three_sigma(anomalies) -> None:
    rows = {r.method.split(" ")[0]: r for r in anomalies["tx_eval"]}
    naive, robust = rows["Naive"], rows["Robust"]
    assert naive.precision < 0.05                     # 3-sigma drowns in false positives
    assert robust.precision > 5 * naive.precision
    assert robust.recall >= 0.70                      # finds most of the 32 injected spikes
    assert robust.f1 > 10 * naive.f1
    assert anomalies["curves"]["z_robust"]["ap"] > anomalies["curves"]["z_naive"]["ap"]


def test_card_level_cluster_rule_recovers_compromised_cards(anomalies) -> None:
    cluster = next(r for r in anomalies["card_eval"] if "robust flags" in r.method)
    naive = next(r for r in anomalies["card_eval"] if "naive" in r.method)
    assert cluster.true_positives >= 7                # of 8 injected spike cards
    assert cluster.flagged < naive.flagged / 10


def test_masking_spikes_inflate_sd_more_than_median(anomalies) -> None:
    m = anomalies["masking"]
    assert len(m) == 8
    assert (m["sd_inflation"] > 1.5).all()
    assert (m["median_shift_pct"].abs() < m["mean_shift_pct"].abs()).all()


def test_naive_rule_matches_sql_query_6_1(con, anomalies) -> None:
    """Python and sql/06_anomalias.sql (6.1) must report the same 3-sigma flags."""
    sql = (SQL_DIR / "06_anomalias.sql").read_text(encoding="utf-8")
    query = sql.split("-- 6.1")[1].split("-- 6.2")[0]
    query = query[query.index("WITH"):].replace("LIMIT 30", "")
    sql_ids = {row[0] for row in con.execute(query)}
    s = anomalies["scores"]
    py_ids = set(s.loc[s["z_naive"] > 3, "transaccion_id"])
    assert sql_ids == py_ids


# --------------------------------------------------------------------------- integration: retention
def test_retention_definition_is_sound(data) -> None:
    from analisis import activation_and_retention

    r = activation_and_retention(data)
    curve = r["curve_reliable"].set_index("month")["retention_pct"]
    assert curve.loc[0] == pytest.approx(100.0)
    # A sound retention curve never rises materially; allow 1 point of noise from cohort mix.
    assert (np.diff(curve.to_numpy()) <= 1.0).all()
    # The phase-1 definition (signup-month cohort) shows the artefact this fixes.
    assert r["legacy"]["m3"] > r["legacy"]["m1"]


def test_sql_retention_matches_python(con, data) -> None:
    from analisis import activation_and_retention

    sql = (SQL_DIR / "05_cohortes_retencion.sql").read_text(encoding="utf-8")
    query = sql.split("-- 5.2")[1].split("-- 5.3")[0]
    query = query[query.index("WITH"):]
    sql_curve = pd.DataFrame(con.execute(query).fetchall(),
                             columns=["month", "eligible", "active", "pct"]).set_index("month")["pct"]
    py_curve = activation_and_retention(data)["curve"].set_index("month")["retention_pct"]
    for k, v in sql_curve.items():
        assert py_curve.loc[k] == pytest.approx(v, abs=0.05)
