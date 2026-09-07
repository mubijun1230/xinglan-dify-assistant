"""Read-only query gateway for Dify natural-language reporting."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pymysql
from flask import Flask, Response, jsonify, request, send_file
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference

from sql_guard import SqlGuardError, sanitize_select

ROOT = Path(__file__).resolve().parent
EXPORT_DIR = ROOT / "exports"
EXPORT_DIR.mkdir(exist_ok=True)
SCHEMA_PATHS = (ROOT / "schema.generated.json", ROOT / "schema.json")
SQL_FENCE = re.compile(r"```(?:sql|json)?\s*(.*?)```", re.S | re.I)
JSON_OBJ = re.compile(r"\{.*\}", re.S)
SELECT_START = re.compile(r"(?is)\b(with|select)\b")
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)

app = Flask(__name__)
_exports: dict[str, dict[str, Any]] = {}


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def load_schema() -> dict[str, Any]:
    for path in SCHEMA_PATHS:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {
        "database": os.environ.get("MYSQL_DATABASE", "njds_data"),
        "table": "nanjiren_cate_date",
        "columns": [],
    }


SCHEMA = load_schema()
TABLE = SCHEMA.get("table") or "nanjiren_cate_date"
COLUMNS = [c["name"] for c in SCHEMA.get("columns", []) if c.get("name")]
MAX_ROWS = int(os.environ.get("MAX_ROWS", "200"))
TOKEN = os.environ.get("QUERY_API_TOKEN", "")
PUBLIC_BASE = os.environ.get("QUERY_PUBLIC_BASE_URL", "http://127.0.0.1:8787").rstrip("/")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ", timespec="seconds") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _query_token() -> str:
    raw = os.environ.get("QUERY_API_TOKEN") or TOKEN or ""
    return raw.strip().strip('"').strip("'")


def _token_matches(got: str, expected: str) -> bool:
    if not got or not expected or len(got) != len(expected):
        return False
    return secrets.compare_digest(got, expected)


def require_token() -> None:
    token = _query_token()
    if not token:
        raise PermissionError("unauthorized")
    header = (request.headers.get("Authorization") or "").strip()
    alt = (request.headers.get("X-Query-Token") or "").strip()
    provided = header[7:].strip() if header.lower().startswith("bearer ") else header
    if provided.lower().startswith("bearer "):
        provided = provided[7:].strip()
    if _token_matches(provided, token) or _token_matches(alt, token):
        return
    raise PermissionError("unauthorized")


def extract_plan(raw: str) -> dict[str, Any]:
    text = THINK_BLOCK.sub(" ", raw or "").strip()
    fenced = SQL_FENCE.findall(text)
    if fenced:
        text = fenced[-1].strip()
    match = JSON_OBJ.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict) and (data.get("sql") or data.get("query")):
                data["sql"] = data.get("sql") or data.get("query")
                return data
        except json.JSONDecodeError:
            pass
    started = SELECT_START.search(text)
    if started:
        sql = text[started.start() :].split("```")[0].strip().rstrip(";")
        return {"sql": sql, "want": "table"}
    raise SqlGuardError("无法从模型输出中解析 SQL")


KNOWN_CHANNELS = ("拼多多", "天猫", "抖音", "京东", "唯品会", "视频号", "快手", "其他", "云集")
KNOWN_CATES = ("内衣", "服饰配件", "服配", "男装", "女装", "鞋品", "童装", "鞋配")
COL_ZH = {
    "month": "月份",
    "year": "年份",
    "channel": "渠道",
    "cate": "类目",
    "internal_cate": "内部类目",
    "pay_amount": "支付金额",
    "amt": "支付金额",
    "amount": "支付金额",
}


def question_year(question: str) -> int:
    q = question or ""
    if re.search(r"2025|去年", q):
        return 2025
    if re.search(r"2024", q):
        return 2024
    return 2026


def wants_by_month(question: str) -> bool:
    q = question or ""
    return bool(re.search(r"按月|每月|各月|分月|逐月", q))


def question_where(question: str) -> str:
    q = question or ""
    cond = [f"year={question_year(q)}"]
    month_span = re.search(r"(\d{1,2})\s*[-~—至到]\s*(\d{1,2})\s*月", q)
    if re.search(r"1\s*[-~—至到]?\s*7\s*月|前7个?月", q) or (
        month_span and month_span.group(1) == "1" and month_span.group(2) == "7"
    ):
        cond.append("month BETWEEN 1 AND 7")
    for channel in KNOWN_CHANNELS:
        if channel in q:
            cond.append(f"channel='{channel}'")
            break
    for cate in sorted(KNOWN_CATES, key=len, reverse=True):
        if cate in q:
            cond.append(f"(cate='{cate}' OR internal_cate='{cate}')")
            break
    return " AND ".join(cond)


def canonical_agg_sql(question: str) -> str:
    where = question_where(question)
    if wants_by_month(question) or re.search(r"1\s*[-~—至到]?\s*7\s*月|前7个?月", question or ""):
        return (
            "SELECT month, ROUND(SUM(pay_amount), 2) AS pay_amount "
            f"FROM nanjiren_cate_date WHERE {where} "
            "GROUP BY month ORDER BY month LIMIT 50"
        )
    if re.search(r"渠道", question or "") and not any(ch in (question or "") for ch in KNOWN_CHANNELS):
        return (
            "SELECT channel, ROUND(SUM(pay_amount), 2) AS pay_amount "
            f"FROM nanjiren_cate_date WHERE {where} "
            "GROUP BY channel ORDER BY pay_amount DESC LIMIT 50"
        )
    return (
        "SELECT ROUND(SUM(pay_amount), 2) AS pay_amount "
        f"FROM nanjiren_cate_date WHERE {where} LIMIT 50"
    )


def select_has_month(sql: str) -> bool:
    head = re.split(r"\bfrom\b", sql or "", maxsplit=1, flags=re.I)[0]
    return bool(re.search(r"\bmonth\b", head, re.I))


def fallback_sql(question: str) -> str | None:
    q = question or ""
    if wants_by_month(q) or re.search(r"1\s*[-~—至到]?\s*7\s*月|前7个?月", q):
        return canonical_agg_sql(q)
    if not re.search(r"渠道", q):
        return None
    if not re.search(r"支付|金额|销售|汇总|合计", q):
        return None
    return canonical_agg_sql(q)


def request_payload() -> tuple[str, str]:
    payload = request.get_json(silent=True) or {}
    if not payload and request.form:
        payload = {key: request.form.get(key) for key in request.form}
    question = str(payload.get("question") or request.args.get("question") or "")
    plan_raw = str(payload.get("sql_draft") or payload.get("plan") or payload.get("sql") or "")
    return question, plan_raw


def detect_want(question: str, plan: dict[str, Any]) -> str:
    q = question or ""
    if re.search(r"图表|柱状图|折线图|饼图|可视化|出图|图标|直方图|柱状", q):
        return "chart"
    if re.search(r"excel|xlsx|导出", q, re.I):
        return "excel"
    return "table"


def pick_amount_col(columns: list[str]) -> str | None:
    for name in ("pay_amount", "amt", "amount", "total"):
        if name in columns:
            return name
    for col in columns:
        if re.search(r"amount|amt|pay|金额", col, re.I):
            return col
    return None


def fmt_wan(value: Any) -> str:
    return f"{float(value or 0) / 10000:.1f}万元"


def format_cell(col: str, value: Any, amount_col: str | None) -> str:
    if col == "month":
        try:
            return f"{int(value)}月"
        except (TypeError, ValueError):
            return str(value or "")
    if amount_col and col == amount_col:
        return fmt_wan(value)
    if value is None:
        return ""
    return str(_json_default(value))


def format_row_label(columns: list[str], row: dict[str, Any], label_col: str) -> str:
    return format_cell(label_col, row.get(label_col), None)


def build_bar_png(columns: list[str], rows: list[dict[str, Any]], title: str) -> bytes | None:
    amount_col = pick_amount_col(columns)
    if not amount_col or not rows:
        return None
    label_col = next((c for c in columns if c != amount_col), columns[0])
    labels = []
    values = []
    for row in rows[:12]:
        amt = float(row.get(amount_col) or 0)
        if amt <= 0:
            continue
        labels.append(format_row_label(columns, row, label_col))
        values.append(amt / 10000)
    if not labels:
        return None
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.font_manager import FontProperties

    font_candidates = [
        ROOT / "fonts" / "simhei.ttf",
        ROOT / "fonts" / "msyh.ttc",
        Path("/app/fonts/simhei.ttf"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    ]
    zh_font = None
    for path in font_candidates:
        if path.exists():
            try:
                font_manager.fontManager.addfont(str(path))
            except Exception:
                pass
            zh_font = FontProperties(fname=str(path))
            plt.rcParams["font.family"] = zh_font.get_name()
            plt.rcParams["font.sans-serif"] = [zh_font.get_name(), "SimHei", "Microsoft YaHei", "DejaVu Sans"]
            break
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=140)
    ax.bar(labels, values, color="#4C7DFF", width=0.62)
    ax.set_title(title or "销售额", fontproperties=zh_font)
    ax.set_ylabel("万元", fontproperties=zh_font)
    ax.set_xlabel("月份" if label_col == "month" else "", fontproperties=zh_font)
    for tick in ax.get_xticklabels():
        tick.set_fontproperties(zh_font)
    for tick in ax.get_yticklabels():
        tick.set_fontproperties(zh_font)
    ax.grid(axis="y", linestyle="--", alpha=0.35)
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def data_as_of(sql: str) -> date:
    year_m = re.search(r"\byear\s*=\s*(\d{4})", sql, re.I)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            if year_m:
                between = re.search(r"month\s+between\s+(\d+)\s+and\s+(\d+)", sql, re.I)
                if between:
                    cur.execute(
                        "SELECT MAX(sta_date) AS d FROM nanjiren_cate_date "
                        "WHERE year=%s AND month BETWEEN %s AND %s",
                        (int(year_m.group(1)), int(between.group(1)), int(between.group(2))),
                    )
                else:
                    cur.execute(
                        "SELECT MAX(sta_date) AS d FROM nanjiren_cate_date WHERE year=%s",
                        (int(year_m.group(1)),),
                    )
            else:
                cur.execute("SELECT MAX(sta_date) AS d FROM nanjiren_cate_date")
            row = cur.fetchone() or {}
            value = row.get("d")
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
    finally:
        conn.close()
    return date(2026, 8, 2)


def excel_filename(question: str, want: str) -> str:
    q = question or ""
    if re.search(r"渠道", q) and re.search(r"今年|2026", q):
        return "本年渠道支付金额汇总.xlsx"
    if re.search(r"渠道", q):
        return "渠道支付金额汇总.xlsx"
    return "查询结果.xlsx"


def build_reply(
    *,
    question: str,
    sql: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    want: str,
    filename: str,
) -> str:
    as_of = data_as_of(sql)
    as_of_s = f"{as_of.year}年{as_of.month:02d}月{as_of.day:02d}日"
    amount_col = pick_amount_col(columns)
    if not rows:
        text = f"截至{as_of_s}，没有符合条件的数据。"
    elif amount_col and len(rows) == 1 and len(columns) == 1:
        text = f"截至{as_of_s}，支付金额合计 {fmt_wan(rows[0].get(amount_col))}。"
    else:
        label_cols = [c for c in columns if c != amount_col] if amount_col else list(columns)
        items: list[str] = []
        total = 0.0
        for row in rows[:50]:
            if amount_col:
                amt = float(row.get(amount_col) or 0)
                if amt <= 0:
                    continue
                total += amt
                labels = [format_cell(c, row.get(c), amount_col) for c in label_cols]
                label = " / ".join(part for part in labels if part) or "合计"
                items.append(f"- {label}：{fmt_wan(amt)}")
            else:
                labels = [f"{COL_ZH.get(c, c)}={format_cell(c, row.get(c), None)}" for c in columns]
                items.append("- " + "，".join(labels))
        if not items:
            text = f"截至{as_of_s}，没有符合条件的数据。"
        else:
            head = f"截至{as_of_s}，汇总如下："
            # Markdown list keeps each month on its own line in Dify/DingTalk.
            text = "\n".join([head, *items])
            if amount_col and len(items) > 1:
                text += f"\n- 合计：{fmt_wan(total)}"
    if want == "excel":
        text += f"\n[{filename}]"
    return text


def markdown_table(columns: list[str], rows: list[dict[str, Any]], limit: int = 30) -> str:
    if not columns:
        return "(无数据)"
    shown = rows[:limit]
    head = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in shown:
        cells = [str(_json_default(row.get(col, ""))).replace("|", "\\|").replace("\n", " ") for col in columns]
        body.append("| " + " | ".join(cells) + " |")
    extra = f"\n\n共 {len(rows)} 行，表中仅展示前 {limit} 行。" if len(rows) > limit else f"\n\n共 {len(rows)} 行。"
    return "\n".join([head, sep, *body]) + extra


def mermaid_chart(columns: list[str], rows: list[dict[str, Any]], chart_type: str, title: str) -> str:
    if len(rows) < 2 or len(columns) < 2:
        return ""
    label_col = columns[0]
    numeric_cols = []
    for col in columns[1:]:
        ok = True
        for row in rows:
            val = row.get(col)
            if val is None or val == "":
                continue
            try:
                float(val)
            except (TypeError, ValueError):
                ok = False
                break
        if ok:
            numeric_cols.append(col)
    if not numeric_cols:
        return ""
    value_col = numeric_cols[0]
    kind = "xychart-beta"
    lines = [f"```mermaid", kind, f'  title "{title or "数据图"}"']
    cats = []
    vals = []
    for row in rows[:20]:
        cats.append(str(_json_default(row.get(label_col, "")))[:16].replace('"', ""))
        try:
            vals.append(str(float(row.get(value_col) or 0)))
        except (TypeError, ValueError):
            vals.append("0")
    lines.append("  x-axis [" + ", ".join(f'"{c}"' for c in cats) + "]")
    lines.append(f'  y-axis "{value_col}"')
    series_kw = "line" if chart_type == "line" else "bar"
    lines.append(f"  {series_kw} [{', '.join(vals)}]")
    lines.append("```")
    return "\n".join(lines)


def build_xlsx(columns: list[str], rows: list[dict[str, Any]], title: str, want: str) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "data"[:31]
    ws.append(columns)
    for row in rows:
        ws.append([_json_default(row.get(col)) for col in columns])
    if want == "chart" and len(columns) >= 2 and len(rows) >= 1:
        numeric_idx = None
        for idx, col in enumerate(columns[1:], start=2):
            try:
                float(rows[0].get(col) or 0)
                numeric_idx = idx
                break
            except (TypeError, ValueError):
                continue
        if numeric_idx:
            chart = LineChart() if "line" in title.lower() else BarChart()
            chart.title = title or "图表"
            data = Reference(ws, min_col=numeric_idx, min_row=1, max_row=len(rows) + 1)
            cats = Reference(ws, min_col=1, min_row=2, max_row=len(rows) + 1)
            chart.add_data(data, titles_from_data=True)
            chart.set_categories(cats)
            ws.add_chart(chart, "H2")
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def connect():
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"],
        connect_timeout=8,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def run_sql(sql: str) -> tuple[list[str], list[dict[str, Any]]]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("SET SESSION MAX_EXECUTION_TIME=8000")
            cur.execute(sql)
            rows = list(cur.fetchall() or [])
            columns = [d[0] for d in (cur.description or [])]
        return columns, rows
    finally:
        conn.close()


def cleanup_exports() -> None:
    now = time.time()
    expired = [key for key, meta in _exports.items() if meta["expires"] < now]
    for key in expired:
        path = Path(_exports[key]["path"])
        if path.exists():
            path.unlink(missing_ok=True)
        _exports.pop(key, None)


@app.get("/health")
def health():
    return {"ok": True, "table": TABLE, "columns": COLUMNS}


@app.post("/query")
def query():
    try:
        require_token()
    except PermissionError:
        return jsonify({"ok": False, "error": "鉴权失败"}), 401

    question, plan_raw = request_payload()
    try:
        try:
            plan = extract_plan(plan_raw)
        except SqlGuardError:
            sql_fb = fallback_sql(question)
            if not sql_fb:
                raise
            plan = {"sql": sql_fb, "title": "按渠道支付金额"}
        sql = sanitize_select(
            str(plan.get("sql") or ""),
            table=TABLE,
            columns=set(COLUMNS) or {"id"},
            max_rows=MAX_ROWS,
        )
        if wants_by_month(question) and not select_has_month(sql):
            sql = sanitize_select(
                canonical_agg_sql(question),
                table=TABLE,
                columns=set(COLUMNS) or {"id"},
                max_rows=MAX_ROWS,
            )
        columns, rows = run_sql(sql)
        want = detect_want(question, plan)
        filename = excel_filename(question, want)
        reply = build_reply(
            question=question,
            sql=sql,
            columns=columns,
            rows=rows,
            want=want,
            filename=filename,
        )
        if want == "excel":
            xlsx = build_xlsx(columns, rows, filename.replace(".xlsx", ""), want)
            cleanup_exports()
            export_id = uuid4().hex
            path = EXPORT_DIR / f"{export_id}.xlsx"
            path.write_bytes(xlsx.getvalue())
            token = secrets.token_urlsafe(16)
            _exports[export_id] = {
                "path": str(path),
                "filename": filename,
                "token": token,
                "expires": time.time() + 15 * 60,
            }
            reply += f"[[DIFY_EXCEL:{export_id}|{token}|{filename}]]"
        elif want == "chart":
            png = build_bar_png(columns, rows, "今年1-7月销售额" if re.search(r"1\s*[-~—至到]?\s*7\s*月", question or "") else "销售额")
            if png:
                cleanup_exports()
                export_id = uuid4().hex
                chart_name = "销售额直方图.png"
                path = EXPORT_DIR / f"{export_id}.png"
                path.write_bytes(png)
                token = secrets.token_urlsafe(16)
                _exports[export_id] = {
                    "path": str(path),
                    "filename": chart_name,
                    "token": token,
                    "expires": time.time() + 15 * 60,
                }
                reply += f"[[DIFY_CHART:{export_id}|{token}|{chart_name}]]"
        return Response(reply, mimetype="text/plain; charset=utf-8")
    except SqlGuardError as exc:
        return Response(str(exc), mimetype="text/plain; charset=utf-8")
    except Exception as exc:  # noqa: BLE001
        return Response(f"查询失败：{type(exc).__name__}", mimetype="text/plain; charset=utf-8")


@app.get("/exports/<export_id>")
def download(export_id: str):
    cleanup_exports()
    meta = _exports.get(export_id)
    if not meta or request.args.get("t") != meta["token"]:
        return "链接无效或已过期", 404
    suffix = Path(meta["filename"]).suffix.lower()
    mime = {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".png": "image/png",
    }.get(suffix, "application/octet-stream")
    return send_file(
        meta["path"],
        as_attachment=True,
        download_name=meta["filename"],
        mimetype=mime,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8787, debug=False)
