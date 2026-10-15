#!/usr/bin/env python3
"""Crea data/transacciones.db (SQLite) y carga los CSV generados.

Uso:  python python/cargar_db.py   (despues de python/generar_datos.py)
"""
import sqlite3
from pathlib import Path

import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"
DB = DATA / "transacciones.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE clientes (
    cliente_id      INTEGER PRIMARY KEY,
    nombre_empresa  TEXT NOT NULL,
    pais            TEXT NOT NULL CHECK (pais IN ('Mexico','Colombia','Brasil')),
    segmento        TEXT NOT NULL CHECK (segmento IN ('Startup','PyME','Corporativo')),
    fecha_alta      DATE NOT NULL
);

CREATE TABLE tarjetas (
    tarjeta_id      INTEGER PRIMARY KEY,
    cliente_id      INTEGER NOT NULL REFERENCES clientes(cliente_id),
    tipo            TEXT NOT NULL CHECK (tipo IN ('fisica','virtual')),
    estatus         TEXT NOT NULL CHECK (estatus IN ('activa','bloqueada','cancelada')),
    fecha_emision   DATE NOT NULL
);

CREATE TABLE transacciones (
    transaccion_id      INTEGER PRIMARY KEY,
    tarjeta_id          INTEGER NOT NULL REFERENCES tarjetas(tarjeta_id),
    cliente_id          INTEGER NOT NULL REFERENCES clientes(cliente_id),
    fecha_hora          DATETIME NOT NULL,
    monto               REAL,
    moneda              TEXT CHECK (moneda IN ('MXN','COP','BRL','USD')),
    categoria_comercio  TEXT CHECK (categoria_comercio IN ('Software','Viajes','Publicidad','Alimentos',
                        'Logistica','Servicios Profesionales','Oficina','Otros')),
    comercio            TEXT,
    pais_comercio       TEXT,
    estatus             TEXT CHECK (estatus IN ('aprobada','rechazada','reversada'))
);

CREATE INDEX idx_tx_tarjeta ON transacciones(tarjeta_id);
CREATE INDEX idx_tx_fecha   ON transacciones(fecha_hora);
CREATE INDEX idx_tx_cliente ON transacciones(cliente_id);
CREATE INDEX idx_tj_cliente ON tarjetas(cliente_id);
"""


def cargar(con, tabla, archivo):
    # keep_default_na=False evita que cadenas como "NA" se lean como nulos; solo '' es NULL
    df = pd.read_csv(DATA / archivo, keep_default_na=False, na_values=[""])
    df = df.astype(object).where(df.notna(), None)
    cols = list(df.columns)
    sql = f"INSERT INTO {tabla} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    con.executemany(sql, df.itertuples(index=False, name=None))
    return len(df)


def main():
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    for tabla, archivo in [("clientes", "clientes.csv"), ("tarjetas", "tarjetas.csv"),
                           ("transacciones", "transacciones.csv")]:
        n = cargar(con, tabla, archivo)
        print(f"{tabla}: {n:,} filas")
    con.commit()
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    print("Violaciones de FK:", len(fk))
    con.execute("ANALYZE")
    con.close()
    print(f"Base creada en {DB}")


if __name__ == "__main__":
    main()
