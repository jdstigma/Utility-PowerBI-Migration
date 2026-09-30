#!/usr/bin/env python3
"""Export every rpt.* view to powerbi_data/<view>.parquet (the SharePoint replacement).

Power BI reads these files straight from the GitHub repo, so each file must stay
well under GitHub's 100 MB limit. The script fails if one doesn't.
Also writes powerbi_data/manifest.json (rows, columns, size, export time).
"""
import json
import time
import warnings
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyodbc

SERVER = r"(localdb)\MSSQLLocalDB"
DB = "UtilityDW"
OUT = Path(__file__).resolve().parent.parent / "powerbi_data"
MAX_MB = 90
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")


def main():
    OUT.mkdir(exist_ok=True)
    cn = pyodbc.connect(f"Driver={{ODBC Driver 18 for SQL Server}};Server={SERVER};Database={DB};"
                        "Trusted_Connection=yes;Encrypt=no;")
    views = [r[0] for r in cn.execute(
        "SELECT name FROM sys.views WHERE schema_id = SCHEMA_ID('rpt') ORDER BY name").fetchall()]
    for stale in OUT.glob("*.parquet"):              # views that were dropped or renamed
        if stale.stem not in views:
            stale.unlink()
            print(f"removed stale {stale.name}")
    manifest = {"exported_at": time.strftime("%Y-%m-%d %H:%M:%S"), "source": f"{DB}.rpt", "files": {}}
    for v in views:
        t0 = time.time()
        path = OUT / f"{v}.parquet"
        writer, rows = None, 0
        for chunk in pd.read_sql(f"SELECT * FROM rpt.[{v}]", cn, chunksize=500_000):
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(path, table.schema, compression="zstd")
            writer.write_table(table.cast(writer.schema))
            rows += len(chunk)
        writer.close()
        mb = path.stat().st_size / 1e6
        manifest["files"][path.name] = {"rows": rows, "columns": table.num_columns, "mb": round(mb, 2)}
        print(f"{path.name:36s} {rows:>10,} rows  {mb:7.2f} MB  {time.time() - t0:6.1f}s")
        if mb > MAX_MB:
            raise SystemExit(f"{path.name} is {mb:.0f} MB; too large for GitHub (limit {MAX_MB} MB)")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    total = sum(f["mb"] for f in manifest["files"].values())
    print(f"exported {len(views)} files, {total:.1f} MB total -> {OUT}")


if __name__ == "__main__":
    main()
