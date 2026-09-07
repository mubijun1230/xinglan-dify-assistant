"""Inspect nanjiren_cate_date and optionally create a SELECT-only user.

Reads credentials from .env in this directory. Does not print passwords.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent


def load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        raise SystemExit("missing query-gateway/.env")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    load_env()
    host = os.environ["MYSQL_HOST"]
    port = int(os.environ.get("MYSQL_PORT", "3306"))
    admin_user = os.environ.get("MYSQL_ADMIN_USER") or os.environ["MYSQL_USER"]
    admin_password = os.environ.get("MYSQL_ADMIN_PASSWORD") or os.environ["MYSQL_PASSWORD"]
    database = os.environ["MYSQL_DATABASE"]
    table = "nanjiren_cate_date"

    conn = pymysql.connect(
        host=host,
        port=port,
        user=admin_user,
        password=admin_password,
        database=database,
        connect_timeout=8,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW FULL COLUMNS FROM `{table}`")
            columns = list(cur.fetchall())
            cur.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
            count = cur.fetchone()["n"]
            cur.execute(f"SELECT * FROM `{table}` LIMIT 3")
            samples = list(cur.fetchall())

            print("query user from .env will be used as-is (prefer a SELECT-only MySQL account)")
    finally:
        conn.close()

    print(f"table={table} rows={count}")
    print("columns:")
    for col in columns:
        print(
            f"  {col.get('Field')}\t{col.get('Type')}\t"
            f"null={col.get('Null')}\tkey={col.get('Key')}\t"
            f"comment={col.get('Comment') or ''}"
        )
    print("sample_rows:")
    print(json.dumps(samples, ensure_ascii=False, default=str, indent=2))

    payload = {
        "database": database,
        "table": table,
        "row_count": count,
        "columns": [
            {
                "name": col.get("Field"),
                "type": str(col.get("Type")),
                "comment": col.get("Comment") or "",
            }
            for col in columns
        ],
    }
    out = ROOT / "schema.generated.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
