#!/usr/bin/env python3
"""Ejecuta un archivo .sql contra data/transacciones.db e imprime los resultados como tabla.

Uso:
    python sql/ejecutar_sql.py sql/02_metricas_mensuales.sql
    python sql/ejecutar_sql.py 03_segmentos_y_categorias.sql --max-filas 100
    python sql/ejecutar_sql.py sql/06_anomalias.sql --db otra.db

Las vistas de 01_limpieza.sql se crean automaticamente si no existen
(al ejecutar 01 directamente, sus sentencias se muestran como cualquier otro archivo).
"""
import argparse
import sqlite3
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
DB_DEFAULT = AQUI.parent / "data" / "transacciones.db"
LIMPIEZA = AQUI / "01_limpieza.sql"


def sentencias(texto):
    """Divide un script en sentencias usando sqlite3.complete_statement (respeta comentarios y cadenas)."""
    buf = ""
    for linea in texto.splitlines(keepends=True):
        buf += linea
        if sqlite3.complete_statement(buf):
            yield buf.strip()
            buf = ""
    resto = "\n".join(l for l in buf.splitlines() if l.strip() and not l.strip().startswith("--"))
    if resto.strip():
        yield buf.strip()


def formatear(v):
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e15 else str(v)
    return str(v)


def imprimir_tabla(cols, filas, max_filas):
    total = len(filas)
    mostrar = filas[:max_filas]
    datos = [[formatear(v) for v in f] for f in mostrar]
    anchos = [max([len(c)] + [len(d[i]) for d in datos]) for i, c in enumerate(cols)]
    sep = "-+-".join("-" * a for a in anchos)
    print(" | ".join(c.ljust(a) for c, a in zip(cols, anchos)))
    print(sep)
    for d in datos:
        celdas = []
        for v, a, orig in zip(d, anchos, range(len(d))):
            num = v.replace(",", "").replace(".", "", 1).lstrip("-").isdigit()
            celdas.append(v.rjust(a) if num else v.ljust(a))
        print(" | ".join(celdas))
    print(f"({total} filas" + (f", mostrando {max_filas}" if total > max_filas else "") + ")")


def ejecutar(con, texto, imprimir=True, max_filas=40):
    n = 0
    for sql in sentencias(texto):
        cur = con.execute(sql)
        if cur.description:
            n += 1
            cols = [d[0] for d in cur.description]
            filas = cur.fetchall()
            if imprimir:
                print(f"\n== Resultado {n} ==")
                imprimir_tabla(cols, filas, max_filas)
    con.commit()
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archivo", help="ruta al .sql (o nombre dentro de sql/)")
    ap.add_argument("--db", default=str(DB_DEFAULT), help="ruta a la base SQLite")
    ap.add_argument("--max-filas", type=int, default=40, help="maximo de filas a mostrar por resultado")
    a = ap.parse_args()

    ruta = Path(a.archivo)
    if not ruta.exists():
        ruta = AQUI / a.archivo
    if not ruta.exists():
        sys.exit(f"No existe el archivo SQL: {a.archivo}")
    if not Path(a.db).exists():
        sys.exit(f"No existe la base {a.db}. Ejecuta python/generar_datos.py y python/cargar_db.py primero.")

    con = sqlite3.connect(a.db)
    if ruta.resolve() != LIMPIEZA.resolve():
        ejecutar(con, LIMPIEZA.read_text(encoding="utf-8"), imprimir=False)
    ejecutar(con, ruta.read_text(encoding="utf-8"), max_filas=a.max_filas)
    con.close()


if __name__ == "__main__":
    main()
