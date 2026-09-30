#!/usr/bin/env python3
"""Build the SQL layers in order: 01_stg.sql -> 02_dw.sql -> 03_rpt.sql (via sqlcmd).

  python sql/build_warehouse.py            # all three
  python sql/build_warehouse.py 03         # just files starting with 03
"""
import subprocess
import sys
import time
from pathlib import Path

SERVER = r"(localdb)\MSSQLLocalDB"
DB = "UtilityDW"
HERE = Path(__file__).parent

files = sorted(HERE.glob("0*.sql"))
if len(sys.argv) > 1:
    files = [f for f in files if any(f.name.startswith(a) for a in sys.argv[1:])]
for f in files:
    t0 = time.time()
    r = subprocess.run(["sqlcmd", "-S", SERVER, "-E", "-d", DB, "-b", "-I", "-x", "-i", str(f)],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print(f"{f.name:14s} {time.time() - t0:7.1f}s  {'ok' if r.returncode == 0 else 'FAILED'}")
    if out:
        print("   " + out.replace("\n", "\n   "))
    if r.returncode:
        sys.exit(r.returncode)
