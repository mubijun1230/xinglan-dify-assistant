"""Allow only a single SELECT on the whitelisted business table."""

from __future__ import annotations

import re

IDENTIFIER = re.compile(r"`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*)")
COMMENT_DASH = re.compile(r"--[^\n]*")
COMMENT_HASH = re.compile(r"#[^\n]*")
COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
LIMIT_RE = re.compile(r"\blimit\s+(\d+)(\s*,\s*\d+)?(\s+offset\s+\d+)?\s*$", re.I)
FORBIDDEN = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|truncate|create|replace|rename|grant|revoke|"
    r"load|outfile|dumpfile|infile|handler|lock|unlock|call|do|set|use|show|"
    r"explain|analyze|optimize|repair|kill|shutdown|prepare|execute|deallocate|"
    r"benchmark|sleep|extractvalue|updatexml|procedure|function|trigger|event|"
    r"information_schema|performance_schema|mysql|sys|into\s+(outfile|dumpfile)"
    r")\b",
    re.I,
)


class SqlGuardError(ValueError):
    pass


def _strip_comments(sql: str) -> str:
    sql = COMMENT_BLOCK.sub(" ", sql)
    sql = COMMENT_DASH.sub(" ", sql)
    sql = COMMENT_HASH.sub(" ", sql)
    return sql


def _identifiers(sql: str) -> list[str]:
    found: list[str] = []
    for match in IDENTIFIER.finditer(sql):
        name = match.group(1) or match.group(2)
        if name:
            found.append(name)
    return found


def sanitize_select(sql: str, *, table: str, columns: set[str], max_rows: int) -> str:
    if not sql or not sql.strip():
        raise SqlGuardError("SQL 为空")
    if "\x00" in sql:
        raise SqlGuardError("非法字符")

    cleaned = _strip_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise SqlGuardError("SQL 为空")
    if ";" in cleaned:
        raise SqlGuardError("禁止多条语句")
    if FORBIDDEN.search(cleaned):
        raise SqlGuardError("只允许只读 SELECT，已拦截危险关键字")
    if re.search(r"\bunion\b", cleaned, re.I):
        raise SqlGuardError("禁止 UNION")
    if re.search(r"\bfrom\s*\(", cleaned, re.I):
        raise SqlGuardError("禁止子查询 FROM")
    if re.search(r"\bjoin\b", cleaned, re.I):
        raise SqlGuardError("禁止 JOIN 其他表")

    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise SqlGuardError("只允许 SELECT / WITH 查询")

    table_l = table.lower()
    if table_l not in {item.lower() for item in _identifiers(cleaned)}:
        raise SqlGuardError(f"只能查询表 {table}")

    for part in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\.", cleaned):
        if part.lower() not in {table_l, "date"}:
            raise SqlGuardError("禁止引用其他库或表")

    from_match = re.search(r"\bfrom\s+(`?[\w.]+`?)", cleaned, re.I)
    if not from_match:
        raise SqlGuardError("缺少 FROM")
    from_table = from_match.group(1).replace("`", "").split(".")[-1].lower()
    if from_table != table_l:
        raise SqlGuardError(f"FROM 只能是 {table}")

    limit_match = LIMIT_RE.search(cleaned)
    if limit_match:
        n = int(limit_match.group(1))
        if n > max_rows:
            cleaned = LIMIT_RE.sub(f"LIMIT {max_rows}", cleaned)
    else:
        cleaned = f"{cleaned} LIMIT {max_rows}"
    return cleaned
