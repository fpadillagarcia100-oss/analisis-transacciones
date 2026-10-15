#!/usr/bin/env python3
"""Automated data-quality checks on the RAW tables (not the cleaned views).

Each check counts the rows that violate one rule, expresses them as a share of
the population it applies to (transactions, cards or clients) and compares that
share with a tolerance threshold. A check *passes* when ``pct <= threshold_pct``.

Outputs:
    reports/calidad_datos.md    human-readable table + summary
    reports/calidad_datos.json  machine-readable results

Usage:
    python python/calidad_datos.py            # report only, exit code 0
    python python/calidad_datos.py --strict   # exit 1 if any CRITICAL check fails
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

from comun import (CATEGORIES, CUTOFF_TS, DB_PATH, FX_PER_USD, LOCAL_CURRENCY, REPORTS_DIR,
                   STATUSES, connect, setup_logging)

log = logging.getLogger("calidad_datos")

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "warning": 1, "info": 2}

# Population queries: the denominator for each kind of check.
POPULATIONS: dict[str, str] = {
    "transactions": "SELECT COUNT(*) FROM transacciones",
    "cards": "SELECT COUNT(*) FROM tarjetas",
    "clients": "SELECT COUNT(*) FROM clientes",
}


def _in_list(values: tuple[str, ...] | list[str]) -> str:
    """Render a Python sequence as a SQL IN-list of string literals (trusted constants only)."""
    return ", ".join(f"'{v}'" for v in values)


@dataclass(frozen=True)
class Check:
    """Definition of a single data-quality rule.

    Attributes:
        name: short snake_case identifier (stable; used by tests and the JSON).
        description: what the rule verifies, in plain words.
        severity: "critical" (breaks metrics), "warning" (degrades them) or "info".
        population: key of POPULATIONS used as the denominator.
        sql: query returning a single integer: the number of affected rows.
        threshold_pct: maximum tolerated share of affected rows, in percent.
    """

    name: str
    description: str
    severity: str
    population: str
    sql: str
    threshold_pct: float


@dataclass
class CheckResult:
    """Outcome of running a Check against the database."""

    name: str
    description: str
    severity: str
    population: str
    affected_rows: int
    population_rows: int
    pct: float
    threshold_pct: float
    passed: bool


def build_checks() -> list[Check]:
    """Return the catalogue of checks, ordered roughly by severity."""
    key_cols = ["tarjeta_id", "cliente_id", "fecha_hora", "monto", "moneda", "categoria_comercio", "estatus"]
    any_key_null = " OR ".join(f"{c} IS NULL" for c in key_cols)
    currency_rule = " OR ".join(
        f"(c.pais = '{country}' AND t.moneda IN ('{cur}', 'USD'))" for country, cur in LOCAL_CURRENCY.items()
    )
    return [
        Check("duplicate_transactions",
              "Same card, timestamp, amount and merchant under a different transaccion_id (extra copies only).",
              "critical", "transactions",
              """
              SELECT COUNT(*) FROM (
                  SELECT ROW_NUMBER() OVER (PARTITION BY tarjeta_id, fecha_hora, monto, comercio
                                            ORDER BY transaccion_id) AS rn
                  FROM transacciones WHERE comercio IS NOT NULL
              ) WHERE rn > 1
              """, 0.1),
        Check("null_key_fields",
              f"NULL in a mandatory column ({', '.join(key_cols)}).",
              "critical", "transactions", f"SELECT COUNT(*) FROM transacciones WHERE {any_key_null}", 0.0),
        Check("negative_amount_not_reversal",
              "Negative amount on an approved/declined row (only reversals may be negative).",
              "critical", "transactions",
              "SELECT COUNT(*) FROM transacciones WHERE monto < 0 AND estatus <> 'reversada'", 0.0),
        Check("future_timestamp",
              f"fecha_hora after the extraction cut-off ({CUTOFF_TS}).",
              "critical", "transactions",
              f"SELECT COUNT(*) FROM transacciones WHERE fecha_hora > '{CUTOFF_TS}'", 0.0),
        Check("orphan_transaction_card",
              "Transaction whose tarjeta_id does not exist in tarjetas.",
              "critical", "transactions",
              """SELECT COUNT(*) FROM transacciones t
                 LEFT JOIN tarjetas j ON j.tarjeta_id = t.tarjeta_id WHERE j.tarjeta_id IS NULL""", 0.0),
        Check("orphan_transaction_client",
              "Transaction whose cliente_id does not exist in clientes.",
              "critical", "transactions",
              """SELECT COUNT(*) FROM transacciones t
                 LEFT JOIN clientes c ON c.cliente_id = t.cliente_id WHERE c.cliente_id IS NULL""", 0.0),
        Check("orphan_card_client",
              "Card whose cliente_id does not exist in clientes.",
              "critical", "cards",
              """SELECT COUNT(*) FROM tarjetas j
                 LEFT JOIN clientes c ON c.cliente_id = j.cliente_id WHERE c.cliente_id IS NULL""", 0.0),
        Check("client_card_mismatch",
              "Transaction's cliente_id differs from the owner of its card.",
              "critical", "transactions",
              """SELECT COUNT(*) FROM transacciones t
                 JOIN tarjetas j ON j.tarjeta_id = t.tarjeta_id WHERE t.cliente_id <> j.cliente_id""", 0.0),
        Check("zero_amount",
              "Amount equal to zero (meaningless for a card payment).",
              "critical", "transactions", "SELECT COUNT(*) FROM transacciones WHERE monto = 0", 0.0),
        Check("currency_domain",
              f"Currency outside the allowed domain ({', '.join(FX_PER_USD)}).",
              "critical", "transactions",
              f"SELECT COUNT(*) FROM transacciones WHERE moneda NOT IN ({_in_list(list(FX_PER_USD))})", 0.0),
        Check("category_domain",
              "Merchant category outside the 8 allowed values.",
              "critical", "transactions",
              f"SELECT COUNT(*) FROM transacciones WHERE categoria_comercio NOT IN ({_in_list(CATEGORIES)})", 0.0),
        Check("status_domain",
              f"Status outside the allowed domain ({', '.join(STATUSES)}).",
              "critical", "transactions",
              f"SELECT COUNT(*) FROM transacciones WHERE estatus NOT IN ({_in_list(STATUSES)})", 0.0),
        Check("null_merchant",
              "Merchant name (comercio) is NULL: the row cannot be attributed or deduplicated.",
              "warning", "transactions", "SELECT COUNT(*) FROM transacciones WHERE comercio IS NULL", 0.1),
        Check("reversal_positive_amount",
              "Reversal recorded with a positive amount (would double-count spend).",
              "warning", "transactions",
              "SELECT COUNT(*) FROM transacciones WHERE estatus = 'reversada' AND monto > 0", 0.0),
        Check("transaction_before_card_issue",
              "Transaction dated before the card's issue date.",
              "warning", "transactions",
              """SELECT COUNT(*) FROM transacciones t JOIN tarjetas j ON j.tarjeta_id = t.tarjeta_id
                 WHERE substr(t.fecha_hora, 1, 10) < j.fecha_emision""", 0.0),
        Check("transaction_before_client_signup",
              "Transaction dated before the client's signup date.",
              "warning", "transactions",
              """SELECT COUNT(*) FROM transacciones t JOIN clientes c ON c.cliente_id = t.cliente_id
                 WHERE substr(t.fecha_hora, 1, 10) < c.fecha_alta""", 0.0),
        Check("currency_country_mismatch",
              "Currency is neither USD nor the local currency of the client's country.",
              "warning", "transactions",
              f"""SELECT COUNT(*) FROM transacciones t JOIN clientes c ON c.cliente_id = t.cliente_id
                  WHERE NOT ({currency_rule})""", 0.0),
        Check("cards_without_transactions",
              "Issued cards with no transaction at all (activation gap, not an error).",
              "info", "cards",
              """SELECT COUNT(*) FROM tarjetas j
                 WHERE NOT EXISTS (SELECT 1 FROM transacciones t WHERE t.tarjeta_id = j.tarjeta_id)""", 5.0),
        Check("clients_without_approved_transactions",
              "Clients that never completed an approved transaction.",
              "info", "clients",
              """SELECT COUNT(*) FROM clientes c
                 WHERE NOT EXISTS (SELECT 1 FROM transacciones t
                                   WHERE t.cliente_id = c.cliente_id AND t.estatus = 'aprobada')""", 5.0),
    ]


def run_checks(con: sqlite3.Connection, checks: list[Check] | None = None) -> list[CheckResult]:
    """Execute every check and return its result (sorted by severity, then name)."""
    checks = checks if checks is not None else build_checks()
    populations = {k: int(con.execute(q).fetchone()[0]) for k, q in POPULATIONS.items()}
    results: list[CheckResult] = []
    for chk in checks:
        affected = int(con.execute(chk.sql).fetchone()[0])
        pop = populations[chk.population]
        pct = 100.0 * affected / pop if pop else 0.0
        results.append(CheckResult(
            name=chk.name, description=chk.description, severity=chk.severity,
            population=chk.population, affected_rows=affected, population_rows=pop,
            pct=round(pct, 4), threshold_pct=chk.threshold_pct, passed=pct <= chk.threshold_pct,
        ))
    results.sort(key=lambda r: (SEVERITY_ORDER[r.severity], r.passed, r.name))
    return results


def usable_rows(con: sqlite3.Connection) -> tuple[int, int]:
    """Return (raw transaction rows, rows that survive the cleaning view)."""
    raw = int(con.execute("SELECT COUNT(*) FROM transacciones").fetchone()[0])
    clean = int(con.execute("SELECT COUNT(*) FROM transacciones_limpias").fetchone()[0])
    return raw, clean


def summarize(results: list[CheckResult], raw: int, clean: int) -> dict:
    """Build the summary block shared by the Markdown and JSON outputs."""
    failed = [r for r in results if not r.passed]
    return {
        "checks_total": len(results),
        "checks_passed": len(results) - len(failed),
        "checks_failed": len(failed),
        "critical_failed": sum(r.severity == "critical" for r in failed),
        "warning_failed": sum(r.severity == "warning" for r in failed),
        "info_failed": sum(r.severity == "info" for r in failed),
        "failed_checks": [r.name for r in failed],
        "raw_transactions": raw,
        "clean_transactions": clean,
        "removed_transactions": raw - clean,
        "removed_pct": round(100.0 * (raw - clean) / raw, 3) if raw else 0.0,
    }


def to_markdown(results: list[CheckResult], summary: dict, generated_at: str) -> str:
    """Render the results as a Markdown report."""
    lines = [
        "# Data quality report",
        "",
        f"_Generated {generated_at} by `python/calidad_datos.py` on the raw tables of `data/transacciones.db`._",
        "",
        "## Summary",
        "",
        f"- **{summary['checks_passed']} of {summary['checks_total']} checks passed**; "
        f"{summary['critical_failed']} critical, {summary['warning_failed']} warning and "
        f"{summary['info_failed']} info checks exceed their threshold.",
        f"- Raw transactions: **{summary['raw_transactions']:,}**. After the cleaning rules in "
        f"`sql/01_limpieza.sql`: **{summary['clean_transactions']:,}** "
        f"({summary['removed_transactions']:,} rows removed, {summary['removed_pct']:.2f}%).",
        "- A check passes when the affected share is at or below its threshold. Thresholds are "
        "tolerances agreed for analytics use: 0% for rules that must never be broken.",
        "- Critical failures distort headline metrics (volume, counts); they are excluded by the "
        "cleaning view and should be fixed at the source system.",
        "",
        "## Results",
        "",
        "| Status | Severity | Check | Affected | Population | % affected | Threshold |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for r in results:
        status = "PASS" if r.passed else "**FAIL**"
        lines.append(
            f"| {status} | {r.severity} | `{r.name}` | {r.affected_rows:,} | "
            f"{r.population_rows:,} {r.population} | {r.pct:.2f}% | {r.threshold_pct:g}% |"
        )
    lines += ["", "## Check definitions", ""]
    lines += [f"- `{r.name}` ({r.severity}): {r.description}" for r in results]
    lines.append("")
    return "\n".join(lines)


def write_reports(results: list[CheckResult], summary: dict, out_dir: Path = REPORTS_DIR) -> tuple[Path, Path]:
    """Write the Markdown and JSON reports; return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    md_path = out_dir / "calidad_datos.md"
    json_path = out_dir / "calidad_datos.json"
    md_path.write_text(to_markdown(results, summary, generated_at), encoding="utf-8")
    payload = {"generated_at": generated_at, "summary": summary, "checks": [asdict(r) for r in results]}
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return md_path, json_path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH, help="path to the SQLite database")
    parser.add_argument("--strict", action="store_true",
                        help="exit with code 1 if any critical check exceeds its threshold")
    args = parser.parse_args(argv)
    setup_logging()

    con = connect(args.db)
    try:
        results = run_checks(con)
        raw, clean = usable_rows(con)
    finally:
        con.close()
    summary = summarize(results, raw, clean)
    md_path, json_path = write_reports(results, summary)

    for r in results:
        level = logging.INFO if r.passed else (logging.ERROR if r.severity == "critical" else logging.WARNING)
        log.log(level, "%-4s %-8s %-38s %7s rows (%.2f%%)", "PASS" if r.passed else "FAIL",
                r.severity, r.name, f"{r.affected_rows:,}", r.pct)
    log.info("%d/%d checks passed; %d critical failures. Reports: %s, %s",
             summary["checks_passed"], summary["checks_total"], summary["critical_failed"],
             md_path.relative_to(md_path.parents[1]), json_path.relative_to(json_path.parents[1]))

    if args.strict and summary["critical_failed"]:
        log.error("--strict: %d critical check(s) failed", summary["critical_failed"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
