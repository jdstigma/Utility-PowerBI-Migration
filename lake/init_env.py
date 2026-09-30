"""Create lake/.env with random credentials for the local S3 emulator (no-op if it exists)."""
import secrets
from pathlib import Path

env = Path(__file__).with_name(".env")
if env.exists():
    print(f"{env} already exists; leaving it alone.")
else:
    env.write_text(
        "# Local S3 emulator credentials (generated; not AWS keys)\n"
        f"ROOT_ACCESS_KEY_ID=local{secrets.token_hex(6)}\n"
        f"ROOT_SECRET_ACCESS_KEY={secrets.token_urlsafe(30)}\n"
    )
    print(f"wrote {env}")
