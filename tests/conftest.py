"""Shared pytest fixtures: make python/ importable and load the dataset once per session."""
from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from comun import DB_PATH, GROUND_TRUTH_PATH, connect  # noqa: E402


@pytest.fixture(scope="session")
def con() -> Iterator[sqlite3.Connection]:
    """Connection to the project database (with the cleaning views)."""
    if not DB_PATH.exists() or not GROUND_TRUTH_PATH.exists():
        pytest.skip("data not generated: run `python python/run_pipeline.py` first")
    connection = connect()
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def ground_truth() -> pd.DataFrame:
    """Row-level ground truth of the injected issues and anomalies."""
    return pd.read_csv(GROUND_TRUTH_PATH)


@pytest.fixture(scope="session")
def data(con: sqlite3.Connection):
    """Cleaned frames used by the analysis module."""
    from analisis import load_data

    return load_data(con)


@pytest.fixture(scope="session")
def anomalies(data, ground_truth: pd.DataFrame) -> dict:
    """Full anomaly-detection run, evaluated against the ground truth."""
    from analisis import anomaly_detection

    return anomaly_detection(data, ground_truth)
