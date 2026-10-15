"""Shared helpers for the phase-2 scripts: repository paths, logging and DB access.

Every script resolves paths from the repository root (not the current working
directory), so they can be run from anywhere, e.g. ``python python/analisis.py``
or ``cd python && python analisis.py``.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = ROOT / "data"
SQL_DIR: Path = ROOT / "sql"
REPORTS_DIR: Path = ROOT / "reports"
FIGURES_DIR: Path = REPORTS_DIR / "figuras"

DB_PATH: Path = DATA_DIR / "transacciones.db"
GROUND_TRUTH_PATH: Path = DATA_DIR / "anomalias_inyectadas.csv"
CLEANING_SQL: Path = SQL_DIR / "01_limpieza.sql"

# Extraction cut-off: anything after this timestamp is a "future" (invalid) date.
CUTOFF_TS: str = "2026-09-30 23:59:59"

# Fixed FX rates (local currency units per 1 USD). Same values as the
# `tasas_cambio` view in sql/01_limpieza.sql; kept here for domain checks.
FX_PER_USD: dict[str, float] = {"MXN": 18.5, "COP": 4100.0, "BRL": 5.4, "USD": 1.0}
LOCAL_CURRENCY: dict[str, str] = {"Mexico": "MXN", "Colombia": "COP", "Brasil": "BRL"}
CATEGORIES: tuple[str, ...] = (
    "Software", "Viajes", "Publicidad", "Alimentos", "Logistica",
    "Servicios Profesionales", "Oficina", "Otros",
)
STATUSES: tuple[str, ...] = ("aprobada", "rechazada", "reversada")

# English display labels for the Spanish values stored in the database.
SEGMENT_LABELS: dict[str, str] = {"Startup": "Startup", "PyME": "SME", "Corporativo": "Corporate"}
COUNTRY_LABELS: dict[str, str] = {"Mexico": "Mexico", "Colombia": "Colombia", "Brasil": "Brazil"}
CATEGORY_LABELS: dict[str, str] = {
    "Software": "Software", "Viajes": "Travel", "Publicidad": "Advertising",
    "Alimentos": "Food & meals", "Logistica": "Logistics",
    "Servicios Profesionales": "Professional services", "Oficina": "Office", "Otros": "Other",
}


def setup_logging(level: int = logging.INFO) -> None:
    """Configure a compact root logger once (idempotent across imports)."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")


def connect(db_path: Path = DB_PATH, create_views: bool = True) -> sqlite3.Connection:
    """Open the SQLite database, optionally (re)creating the cleaning views.

    The views (`tasas_cambio`, `transacciones_limpias`, `transacciones_aprobadas`)
    are defined in sql/01_limpieza.sql, which is the single source of truth for
    the cleaning rules; we execute that file instead of duplicating its logic.
    """
    if not db_path.exists():
        raise FileNotFoundError(
            f"Database not found: {db_path}. Run python/generar_datos.py and python/cargar_db.py first."
        )
    con = sqlite3.connect(db_path)
    if create_views:
        # executescript runs every statement; the trailing verification SELECT is harmless.
        con.executescript(CLEANING_SQL.read_text(encoding="utf-8"))
    return con
