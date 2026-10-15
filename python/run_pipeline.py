#!/usr/bin/env python3
"""Run the whole project end to end with one command.

Steps:
    1. generate   python/generar_datos.py      synthetic CSVs + ground truth (seed 42)
    2. load       python/cargar_db.py          SQLite database data/transacciones.db
    3. sql        sql/*.sql                    smoke test: every SQL script must execute
    4. quality    python/calidad_datos.py      data-quality report (reports/calidad_datos.*)
    5. analysis   python/analisis.py           figures, hallazgos.md, metricas.json
    6. dashboard  python/exportar_dashboard.py only if that file exists

Usage:
    python python/run_pipeline.py                  # full run
    python python/run_pipeline.py --skip-generate  # reuse the existing database
    python python/run_pipeline.py --strict         # fail if a critical quality check fails
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from comun import DATA_DIR, DB_PATH, ROOT, SQL_DIR, connect, setup_logging

log = logging.getLogger("pipeline")
PY_DIR = ROOT / "python"


@dataclass
class StepResult:
    """Outcome of one pipeline step."""

    name: str
    status: str          # "ok", "skipped" or "failed"
    seconds: float
    detail: str = ""


def run_script(script: Path, *args: str) -> None:
    """Run a project script with the current interpreter; raise if it fails."""
    cmd = [sys.executable, str(script), *args]
    completed = subprocess.run(cmd, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"{script.relative_to(ROOT)} exited with code {completed.returncode}")


def validate_sql() -> str:
    """Execute every sql/*.sql file against the database (results discarded)."""
    spec = importlib.util.spec_from_file_location("ejecutar_sql", SQL_DIR / "ejecutar_sql.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import sql/ejecutar_sql.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    files = sorted(SQL_DIR.glob("*.sql"))
    con = connect()  # also (re)creates the views from 01_limpieza.sql
    try:
        n_results = sum(runner.ejecutar(con, f.read_text(encoding="utf-8"), imprimir=False) for f in files)
    finally:
        con.close()
    return f"{len(files)} files, {n_results} result sets"


def timed(name: str, fn: Callable[[], str | None]) -> StepResult:
    """Run a step, log its duration and convert exceptions into a failed result."""
    log.info("--- %s ---", name)
    start = time.perf_counter()
    try:
        detail = fn() or ""
        return StepResult(name, "ok", time.perf_counter() - start, detail)
    except Exception as exc:  # noqa: BLE001 - any failure stops the pipeline with a clear message
        log.error("%s failed: %s", name, exc)
        return StepResult(name, "failed", time.perf_counter() - start, str(exc))


def print_summary(results: list[StepResult], total: float) -> None:
    """Concise, aligned summary of every step."""
    width = max(len(r.name) for r in results)
    print("\nPipeline summary")
    print("-" * (width + 40))
    for r in results:
        print(f"{r.name:<{width}}  {r.status:<8} {r.seconds:6.1f}s  {r.detail}")
    print("-" * (width + 40))
    print(f"{'total':<{width}}  {'':<8} {total:6.1f}s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-generate", action="store_true",
                        help="reuse the existing database instead of regenerating and reloading the data")
    parser.add_argument("--strict", action="store_true",
                        help="fail the pipeline if any critical data-quality check exceeds its threshold")
    parser.add_argument("--no-dashboard", action="store_true", help="do not run python/exportar_dashboard.py")
    args = parser.parse_args(argv)
    setup_logging()

    steps: list[tuple[str, Callable[[], str | None] | None, str]] = []
    if args.skip_generate and DB_PATH.exists():
        steps.append(("generate", None, "skipped (--skip-generate)"))
        steps.append(("load", None, "skipped (database exists)"))
    elif args.skip_generate and (DATA_DIR / "transacciones.csv").exists():
        log.warning("--skip-generate: database missing, loading it from the existing CSVs")
        steps.append(("generate", None, "skipped (--skip-generate)"))
        steps.append(("load", lambda: run_script(PY_DIR / "cargar_db.py"), ""))
    else:
        if args.skip_generate:
            log.warning("--skip-generate ignored: no database or CSVs found")
        steps.append(("generate", lambda: run_script(PY_DIR / "generar_datos.py"), ""))
        steps.append(("load", lambda: run_script(PY_DIR / "cargar_db.py"), ""))

    steps.append(("sql", validate_sql, ""))
    steps.append(("quality", lambda: run_script(PY_DIR / "calidad_datos.py", *(["--strict"] if args.strict else [])),
                  ""))
    steps.append(("analysis", lambda: run_script(PY_DIR / "analisis.py"), ""))

    dashboard = PY_DIR / "exportar_dashboard.py"
    if args.no_dashboard:
        steps.append(("dashboard", None, "skipped (--no-dashboard)"))
    elif dashboard.exists():
        steps.append(("dashboard", lambda: run_script(dashboard), ""))
    else:
        steps.append(("dashboard", None, "skipped (python/exportar_dashboard.py not found)"))

    results: list[StepResult] = []
    t0 = time.perf_counter()
    for name, fn, note in steps:
        if fn is None:
            results.append(StepResult(name, "skipped", 0.0, note))
            continue
        res = timed(name, fn)
        results.append(res)
        if res.status == "failed":
            break
    print_summary(results, time.perf_counter() - t0)
    return 1 if any(r.status == "failed" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
