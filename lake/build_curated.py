#!/usr/bin/env python3
"""Raw zone -> curated zone (the Athena CTAS step), run with DuckDB against the lake.

s3://BUCKET/raw/<system>/<table>/...csv.gz  ->  s3://BUCKET/curated/<system>/<table>/...parquet

Everything is read as text, then typed by column name:
  * SAP YYYYMMDD integers become DATE ('0' -> NULL, 99991231 kept as open-ended)
  * ids/counts -> BIGINT, money -> DECIMAL(14,2), measures -> DOUBLE, ISO timestamps -> TIMESTAMP
  * everything else stays VARCHAR exactly as delivered (ZIPs, phones, flags, codes)
Data-quality problems are intentionally NOT fixed here; that's the SQL staging layer's job.
Tables landed with year_month=... partitions keep them.
"""
import json
import time

import config

SAP_DATES = {"CRDAT", "ZAUTOPAY_DATE", "ZEBILL_DATE", "EINZDAT", "AUSZDAT", "INBDT", "BUDAT", "BEGABRPE",
             "ENDABRPE", "FAEDN", "STORNODAT", "ERDAT", "SCHED_DATE", "COMPL_DATE", "GSTRP", "GLTRP", "GETRI"}
BIGINTS = {"PARTNER", "VKONT", "GPART", "VSTELLE", "HAUS", "ADDRNUMBER", "ANLAGE", "VERTRAG", "VKONTO", "EQUNR",
           "BELNR", "PAYMENT_ID", "PLAN_ID", "ORDER_ID", "AUFNR", "EVENT_ID", "INTERACTION_ID", "BP_ID",
           "METER_ID", "HOUSE_NUM1", "BAUJJ", "NUM_INSTALLMENTS", "CUSTOMERS_OUT", "CUSTOMER_MINUTES",
           "WAIT_SEC", "HANDLE_SEC", "CSAT", "MAX_GUST_MPH"}
MONEY = {"ELEC_DELIV_AMT", "ELEC_SUPPLY_AMT", "GAS_DELIV_AMT", "GAS_SUPPLY_AMT", "TAX_AMT", "TOTAL_AMT",
         "BETRZ", "PLAN_COST", "ACT_COST", "DOWN_PAYMENT", "CUSTOMER_CHARGE"}
DOUBLES = {"KWH", "THERMS", "PEAK_KW", "GEO_LAT", "GEO_LON", "LAT", "LON", "TAVG_F", "TMAX_F", "TMIN_F",
           "PRECIP_IN", "HDD", "CDD", "DELIVERY_PER_UNIT", "SUPPLY_PER_UNIT"}
TIMESTAMPS = {"EVENT_START", "RESTORE_TIME", "CREATED_TS"}
ISO_DATES = {"READ_DATE", "OBS_DATE"}


def expr(col):
    c = f'"{col}"'
    v = f"NULLIF({c}, '')"
    if col in SAP_DATES:
        return f"CASE WHEN {v} IS NULL OR {c} = '0' THEN NULL ELSE strptime({c}, '%Y%m%d')::DATE END AS {c}"
    if col == "ERZEIT":
        return f"strptime(lpad({c}, 6, '0'), '%H%M%S')::TIME AS {c}"
    for names, typ in ((BIGINTS, "BIGINT"), (MONEY, "DECIMAL(14,2)"), (DOUBLES, "DOUBLE"),
                       (TIMESTAMPS, "TIMESTAMP"), (ISO_DATES, "DATE")):
        if col in names:
            return f"CAST({v} AS {typ}) AS {c}"
    return c


def clear_prefix(s3, prefix):
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config.BUCKET, Prefix=prefix):
        keys += [{"Key": o["Key"]} for o in page.get("Contents", [])]
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=config.BUCKET, Delete={"Objects": keys[i:i + 1000]})


def main():
    s3 = config.s3_client()
    raw_manifest = json.loads(s3.get_object(Bucket=config.BUCKET, Key="raw/_manifest.json")["Body"].read())
    con = config.duckdb_connect()
    con.execute("SET preserve_insertion_order = false; SET threads = 8;")
    out = {}
    for table, expected in raw_manifest["row_counts"].items():
        t0 = time.time()
        src = f"s3://{config.BUCKET}/raw/{table}/**/*.csv.gz"
        dest = f"s3://{config.BUCKET}/curated/{table}"
        first = s3.list_objects_v2(Bucket=config.BUCKET, Prefix=f"raw/{table}/", MaxKeys=1).get("Contents", [])
        partitioned = bool(first) and "/year_month=" in first[0]["Key"]
        read = f"read_csv('{src}', all_varchar = true, header = true, hive_partitioning = {str(partitioned).lower()})"
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {read}").fetchall()]
        select = ", ".join(expr(c) for c in cols if c != "year_month")
        clear_prefix(s3, f"curated/{table}/")
        if partitioned:
            con.execute(f"""COPY (SELECT {select}, year_month FROM {read})
                            TO '{dest}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (year_month))""")
        else:
            con.execute(f"COPY (SELECT {select} FROM {read}) TO '{dest}/data.parquet' (FORMAT parquet, COMPRESSION zstd)")
        got = con.execute(f"SELECT count(*) FROM read_parquet('{dest}/**/*.parquet')").fetchone()[0]
        status = "ok" if got == expected else f"MISMATCH (raw {expected:,})"
        print(f"{table:28s} {got:>12,} rows  {time.time() - t0:6.1f}s  {status}", flush=True)
        if got != expected:
            raise SystemExit(f"row count mismatch for {table}")
        out[table] = got
    s3.put_object(Bucket=config.BUCKET, Key="curated/_manifest.json",
                  Body=json.dumps({"source": raw_manifest, "row_counts": out}, indent=2).encode())
    print(f"curated zone complete: {sum(out.values()):,} rows")


if __name__ == "__main__":
    main()
