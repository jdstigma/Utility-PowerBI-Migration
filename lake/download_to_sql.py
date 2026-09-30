#!/usr/bin/env python3
"""Blob download: lake curated zone -> SQL Server landing schemas.

  1. Download curated Parquet from s3://BUCKET/curated/ into LANDING_DIR (skips unchanged files)
  2. DuckDB turns each table into one UTF-8 CSV and infers SQL Server column types/lengths
  3. Recreate <system>.<table> in UtilityDW and BULK INSERT it
     (clustered columnstore for large tables so ~50M rows fit comfortably in LocalDB/Express)
  4. Verify row counts against the curated manifest and record the load in etl.load_log

  python lake/download_to_sql.py                 # all tables
  python lake/download_to_sql.py sap_isu/ERCH    # just one
"""
import json
import shutil
import sys
import time
from pathlib import Path

import pyodbc

import config

SERVER = r"(localdb)\MSSQLLocalDB"
DB = "UtilityDW"
DB_DIR = config.DATA_DIR / "sqldb"
COLUMNSTORE_MIN_ROWS = 1_000_000

TYPE_MAP = {"BIGINT": "BIGINT", "DOUBLE": "FLOAT", "DATE": "DATE", "TIME": "TIME(0)",
            "TIMESTAMP": "DATETIME2(0)"}


def connect(database="master"):
    cs = (f"Driver={{ODBC Driver 18 for SQL Server}};Server={SERVER};Database={database};"
          "Trusted_Connection=yes;Encrypt=no;")
    return pyodbc.connect(cs, autocommit=True)


def ensure_database():
    with connect() as cn:
        if cn.execute("SELECT DB_ID(?)", DB).fetchone()[0] is None:
            DB_DIR.mkdir(parents=True, exist_ok=True)
            cn.execute(f"""
                CREATE DATABASE [{DB}]
                ON (NAME = {DB}_data, FILENAME = '{DB_DIR / (DB + '.mdf')}', SIZE = 512MB, FILEGROWTH = 256MB)
                LOG ON (NAME = {DB}_log, FILENAME = '{DB_DIR / (DB + '_log.ldf')}', SIZE = 256MB, FILEGROWTH = 256MB)""")
            cn.execute(f"ALTER DATABASE [{DB}] SET RECOVERY SIMPLE")
            print(f"created database {DB} in {DB_DIR}")
    with connect(DB) as cn:
        cn.execute("IF SCHEMA_ID('etl') IS NULL EXEC('CREATE SCHEMA etl')")
        cn.execute("""
            IF OBJECT_ID('etl.load_log') IS NULL
            CREATE TABLE etl.load_log (
                load_id      INT IDENTITY PRIMARY KEY,
                table_name   SYSNAME       NOT NULL,
                source_path  NVARCHAR(400) NOT NULL,
                source_files INT           NOT NULL,
                rows_expected BIGINT       NOT NULL,
                rows_loaded  BIGINT        NOT NULL,
                started_at   DATETIME2(0)  NOT NULL,
                finished_at  DATETIME2(0)  NOT NULL,
                status       VARCHAR(20)   NOT NULL)""")


def download(s3, table):
    """Blob download of one curated table. Returns (local dir, file count)."""
    local = config.LANDING_DIR / "curated" / table
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config.BUCKET, Prefix=f"curated/{table}/"):
        for o in page.get("Contents", []):
            dest = config.LANDING_DIR / o["Key"]
            n += 1
            if dest.exists() and dest.stat().st_size == o["Size"]:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(config.BUCKET, o["Key"], str(dest))
    return local, n


def sql_columns(con, src):
    desc = con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
    varchars = [c for c, t, *_ in desc if t == "VARCHAR"]
    lens = {}
    if varchars:
        row = con.execute("SELECT " + ", ".join(f'max(length("{c}"))' for c in varchars) + f" FROM {src}").fetchone()
        lens = dict(zip(varchars, row))
    cols = []
    for c, t, *_ in desc:
        if t == "VARCHAR":
            n = max(int(lens[c] or 1), 1)
            st = f"VARCHAR({min(max(8, -(-n // 10) * 10), 8000)})"
        elif t.startswith("DECIMAL"):
            st = t
        else:
            st = TYPE_MAP[t]
        cols.append((c, st))
    return cols


def load_table(s3, duck, table, expected):
    t0 = time.time()
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    local, nfiles = download(s3, table)
    partitioned = any(p.parent.name.startswith("year_month=") for p in local.rglob("*.parquet"))
    src = (f"read_parquet('{local.as_posix()}/**/*.parquet', hive_partitioning = {str(partitioned).lower()})")
    cols = sql_columns(duck, src)
    csv = config.LANDING_DIR / "_csv" / (table.replace("/", "__") + ".csv")
    csv.parent.mkdir(parents=True, exist_ok=True)
    duck.execute(f"COPY (SELECT * FROM {src}) TO '{csv.as_posix()}' (FORMAT csv, HEADER true, QUOTE '\"')")

    schema, name = table.split("/")
    fq = f"[{schema}].[{name}]"
    with connect(DB) as cn:
        cn.execute(f"IF SCHEMA_ID('{schema}') IS NULL EXEC('CREATE SCHEMA [{schema}]')")
        cn.execute(f"DROP TABLE IF EXISTS {fq}")
        cn.execute(f"CREATE TABLE {fq} (" + ", ".join(f"[{c}] {t} NULL" for c, t in cols) + ")")
        if expected >= COLUMNSTORE_MIN_ROWS:
            cn.execute(f"CREATE CLUSTERED COLUMNSTORE INDEX CCI_{name} ON {fq}")
        cn.execute(f"""
            BULK INSERT {fq} FROM '{csv}'
            WITH (FORMAT = 'CSV', FIRSTROW = 2, FIELDQUOTE = '"', FIELDTERMINATOR = ',',
                  ROWTERMINATOR = '0x0a', CODEPAGE = '65001', KEEPNULLS, TABLOCK, BATCHSIZE = 1048576)""")
        got = cn.execute(f"SELECT COUNT_BIG(*) FROM {fq}").fetchone()[0]
        status = "ok" if got == expected else "MISMATCH"
        cn.execute("""INSERT etl.load_log (table_name, source_path, source_files, rows_expected, rows_loaded,
                                           started_at, finished_at, status)
                      VALUES (?, ?, ?, ?, ?, ?, SYSDATETIME(), ?)""",
                   f"{schema}.{name}", f"s3://{config.BUCKET}/curated/{table}/", nfiles, expected, got, started, status)
    csv.unlink()
    print(f"{schema + '.' + name:28s} {got:>12,} rows  {nfiles:>3} files  {time.time() - t0:6.1f}s  {status}", flush=True)
    if got != expected:
        raise SystemExit(f"row count mismatch for {table}: expected {expected:,}, loaded {got:,}")


def main():
    import duckdb

    s3 = config.s3_client()
    manifest = json.loads(s3.get_object(Bucket=config.BUCKET, Key="curated/_manifest.json")["Body"].read())
    tables = manifest["row_counts"]
    only = sys.argv[1:]
    if only:
        tables = {t: tables[t] for t in only}
    ensure_database()
    duck = duckdb.connect()
    duck.execute("SET enable_progress_bar = false; SET preserve_insertion_order = false;")
    t0 = time.time()
    for table, expected in tables.items():
        load_table(s3, duck, table, expected)
    shutil.rmtree(config.LANDING_DIR / "_csv", ignore_errors=True)
    with connect(DB) as cn:
        mb = cn.execute("SELECT SUM(size) * 8 / 1024 FROM sys.database_files WHERE type = 0").fetchone()[0]
    print(f"loaded {len(tables)} tables in {time.time() - t0:.0f}s; {DB} data file {mb:,} MB")


if __name__ == "__main__":
    main()
