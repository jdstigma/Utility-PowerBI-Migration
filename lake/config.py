"""Shared lake settings.

Point the pipeline at real AWS instead of the local emulator by setting
S3_ENDPOINT_URL to an empty string and using normal AWS credentials/profile.
"""
import os
from pathlib import Path

HERE = Path(__file__).parent


def _load_env(path=HERE / ".env"):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

DATA_DIR = Path(os.environ.get("UTILITY_DATA_DIR", r"C:\Data\utility-powerbi"))
RAW_DIR = DATA_DIR / "raw"                     # generator output (the source-system drop zone)
LANDING_DIR = DATA_DIR / "landing"             # blob downloads from the lake, before SQL load

BUCKET = os.environ.get("LAKE_BUCKET", "utility-lake")
ENDPOINT = os.environ.get("S3_ENDPOINT_URL", "http://localhost:7070") or None
REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCESS_KEY = os.environ.get("ROOT_ACCESS_KEY_ID")
SECRET_KEY = os.environ.get("ROOT_SECRET_ACCESS_KEY")


def s3_client():
    import boto3
    from botocore.config import Config

    kw = {"region_name": REGION, "config": Config(retries={"max_attempts": 5, "mode": "standard"},
                                                   s3={"addressing_style": "path"})}
    if ENDPOINT:
        kw.update(endpoint_url=ENDPOINT, aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY)
    return boto3.client("s3", **kw)


def duckdb_connect(path=":memory:"):
    """DuckDB connection that can read/write s3://BUCKET/... on the configured endpoint."""
    import duckdb
    from urllib.parse import urlparse

    con = duckdb.connect(path)
    con.execute("INSTALL httpfs; LOAD httpfs;")
    if ENDPOINT:
        u = urlparse(ENDPOINT)
        con.execute(f"""
            CREATE OR REPLACE SECRET lake (
                TYPE s3, KEY_ID '{ACCESS_KEY}', SECRET '{SECRET_KEY}', REGION '{REGION}',
                ENDPOINT '{u.netloc}', URL_STYLE 'path', USE_SSL {str(u.scheme == 'https').lower()})""")
    else:
        con.execute("CREATE OR REPLACE SECRET lake (TYPE s3, PROVIDER credential_chain)")
    return con
