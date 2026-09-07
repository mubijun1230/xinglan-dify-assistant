"""Patch 业务助手: 正式工作流（意图分流、空检索拒答、取数失败兜底）。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app_factory import create_app
from extensions.ext_database import db
from models.model import App
from models.workflow import Workflow

APP_ID = "27f263c1-5577-4718-9559-fafda54d3bc0"
START_ID = "1776072202913"
DS_OA = "eb6da1b9-1109-4b84-beb0-68c38e20ead3"
DS_PROJECT = "3124f62f-5f7c-44f2-8656-3e306845a58e"
DS_POLICY = "60060625-6909-4d73-9e25-571106a5b113"

PROVIDER = "langgenius/siliconflow/siliconflow"
MODEL_NAME = "deepseek-ai/DeepSeek-V4-Flash"


def _load_query_api_token() -> str:
    token = os.environ.get("QUERY_API_TOKEN", "").strip()
    env_path = Path("/tmp/query-gateway/.env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "QUERY_API_TOKEN":
                token = value.strip().strip('"').strip("'")
                break
    return token or "CHANGE_ME_QUERY_TOKEN"


QUERY_API_TOKEN = _load_query_api_token()

# 回复口径（空检索 / 职责外 / 网关失败）：不经二次模型改写，避免编造。
MSG_OA_MISS = (
    "未在现行 OA 流程与规章制度中检索到与该问题直接对应的条款，"
    "助手不能口头补全审批人、时限或金额。\n"
    "请登录南极 OA 办理，或联系人力资源 / 行政 / 财务 / 法务。"
)
MSG_PROJECT_MISS = (
    "工程项目档案未收录该工程名称或字段，不能编造负责人或上线日期。\n"
    "请向信息化 / PMO 补录后再查。"
)
MSG_SQL_FAIL = (
    "经营取数未成功返回（网关不可用或执行失败）。请稍后重试；"
    "持续失败请联系数据管理员。在结果确认前，请勿用猜测数字做经营决策。"
)
MSG_OOS = (
    "超出南极业务助手范围。仅支持：OA与制度、内部工程项目、类目日销只读取数。"
)

INTENT_INSTRUCTION = """你是南极人「南极业务助手」意图分类器。只输出类别。
结合最近几轮理解指代（「那个工程」「还要谁批」「导出刚才那个」）。

分类规则（互斥，按优先级）：
1. sql：要从业务库 nanjiren_cate_date 取支付金额、渠道、类目、店铺、实控人/负责人、按日/月/年汇总、导出 Excel、直方图。问的是报表数字，不是档案里写死的回款风险描述。
2. project：内部工程名称/编号（日销仓、NJ-DATA-01、渠道中台、OA审批二期、仓储WMS、内衣供应链）、负责人、状态、里程碑。不是支付金额汇总。
3. oa：公司内部流程与制度。请假考勤、差旅、报销付款、采购、合同用印、入职离职、IT 账号、加班、会议室、立项、固定资产、员工手册、信息安全、反贿赂、财务纪律、招聘培训。
4. chitchat：打招呼、你是谁、谢谢、能力范围介绍。
5. oos：与以上四类都无关，或要求改库、绕过审批、猜测未建档信息、闲聊八卦、外部通用知识（天气、写代码、股票推荐等）。

边界（必须遵守）：
- 「某工程负责人/状态」→ project；「今年各渠道支付金额」→ sql。
- 立项审批怎么走 → oa；NJ-OA-03 工程状态 → project。
- 流程+取数同时出现：有金额/渠道/类目 → sql，否则 oa。
"""

PROMPT_OA = """你是南极业务助手，按现行制度与 OA 答复，不替代审批。

工作标准：
1. 只依据下方检索片段作答，禁止用常识补全审批人、金额、时限、附件清单。
2. 回复结构固定为四段：结论 / 依据（文档名或条款编号）/ 办理路径（哪个 OA、谁归口）/ 注意事项（空缺则写「检索未覆盖，请以 OA 页面为准」）。
3. 检索片段不足以回答时，明确说未覆盖，引导走 OA 或归口部门，不要编条款。
4. 简体中文，条目清晰，适合钉钉转发。不要输出思考过程。不要使用 markdown 标题。

{{#context#}}
"""

PROMPT_PROJECT = """你是南极业务助手，按内部工程项目档案答复。

工作标准：
1. 只依据下方检索片段作答，禁止编造负责人、状态、上线日期。
2. 回复结构固定为：结论 / 依据（档案文档名）/ 风险或待办 / 下一步（谁补录、谁确认）。
3. 档案未收录的字段必须写「档案未收录」，请信息化 / PMO 补录，不得猜测。
4. 简体中文，条目清晰。不要输出思考过程。不要使用 markdown 标题。

{{#context#}}
"""

PROMPT_SQL_PLAN = """你是南极人类目日销 SQL 生成器。业务日 2026-09-07。
只输出一条 MySQL SELECT，不要 JSON，不要 markdown，不要解释，不要思考过程。
只能查表 nanjiren_cate_date。禁止 INSERT/UPDATE/DELETE/DROP/UNION/JOIN/多语句/注释/其他表。

字段：id, year, month, day, sta_date(统计日), cate, internal_cate, channel, master, manager, store_id, store_name, judg, type, oa_apply_type, pay_amount(支付金额，单位元), create_time(入库时间，禁止当业务日期)。
渠道取值：拼多多、天猫、抖音、京东、唯品会、视频号、快手、其他、云集。
类目取值：内衣、服饰配件、服配、男装、女装、鞋品、童装、鞋配。

规则：
- 今年 = year=2026；去年 = year=2025。时间过滤只用 year / month / day / sta_date。
- 按渠道汇总：SELECT channel, ROUND(SUM(pay_amount),2) AS pay_amount FROM nanjiren_cate_date WHERE year=2026 GROUP BY channel ORDER BY pay_amount DESC LIMIT 50
- 今年1-7月按月：SELECT month, ROUND(SUM(pay_amount),2) AS pay_amount FROM nanjiren_cate_date WHERE year=2026 AND month BETWEEN 1 AND 7 GROUP BY month ORDER BY month LIMIT 50
- 按月+渠道+类目必须选出 month：SELECT month, ROUND(SUM(pay_amount),2) AS pay_amount FROM nanjiren_cate_date WHERE year=2025 AND channel='抖音' AND cate='内衣' GROUP BY month ORDER BY month LIMIT 50
- 问「按月」时 SELECT 列表必须包含 month，禁止只 SELECT SUM(pay_amount)
- 金额用 SUM(pay_amount)；必须有 LIMIT（聚合最多 50，明细最多 100）
- 不要输出图表代码，图表由取数网关生成
"""

PROMPT_CHAT = """你是南极业务助手。一两句说明：可查 OA/制度、内部工程项目、类目日销只读取数。
不要编造数字或条款。不要输出思考过程。用户若提出职责外问题，引导改问上述三类。
"""

FEATURES = {
    "opening_statement": (
        "你好，我是南极业务助手。可查 OA 与制度、内部工程项目，"
        "以及类目日销只读取数（汇总、导出 Excel、直方图）。"
        "制度与档案以知识库为准，数字以取数网关回传为准；未检索到的内容不会口头补全。"
    ),
    "suggested_questions": [
        "请假超过 3 天要谁审批？",
        "日销仓项目的负责人是谁？",
        "2025年抖音渠道内衣支付金额多少？",
        "按渠道汇总今年支付金额并导出Excel",
        "按渠道汇总今年1-7月支付金额并用直方图展示",
    ],
    "suggested_questions_after_answer": {"enabled": True},
    "text_to_speech": {"enabled": False, "language": "", "voice": ""},
    "speech_to_text": {"enabled": False},
    "retriever_resource": {"enabled": True},
    "sensitive_word_avoidance": {"enabled": False},
    "file_upload": {
        "image": {"enabled": False, "number_limits": 3, "transfer_methods": ["local_file", "remote_url"]},
        "enabled": False,
        "allowed_file_types": ["image"],
        "allowed_file_extensions": [".JPG", ".JPEG", ".PNG", ".GIF", ".WEBP", ".SVG"],
        "allowed_file_upload_methods": ["local_file", "remote_url"],
        "number_limits": 3,
    },
}


def _model(temperature: float, max_tokens: int) -> dict:
    return {
        "provider": PROVIDER,
        "name": MODEL_NAME,
        "mode": "chat",
        "completion_params": {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "enable_thinking": False,
        },
    }


def _custom_node(node_id: str, data: dict, x: float, y: float, width: int = 242, height: int = 140) -> dict:
    return {
        "id": node_id,
        "type": "custom",
        "data": data,
        "position": {"x": x, "y": y},
        "positionAbsolute": {"x": x, "y": y},
        "width": width,
        "height": height,
        "sourcePosition": "right",
        "targetPosition": "left",
        "selected": False,
        "zIndex": 0,
    }


def _edge(source: str, target: str, source_type: str, target_type: str, source_handle: str = "source") -> dict:
    return {
        "id": f"{source}-{source_handle}-{target}-target",
        "type": "custom",
        "source": source,
        "sourceHandle": source_handle,
        "target": target,
        "targetHandle": "target",
        "data": {
            "isInIteration": False,
            "isInLoop": False,
            "sourceType": source_type,
            "targetType": target_type,
        },
        "zIndex": 0,
        "selected": False,
    }


def _kr_node(node_id: str, title: str, dataset_ids: list[str], x: float, y: float) -> dict:
    return _custom_node(
        node_id,
        {
            "type": "knowledge-retrieval",
            "title": title,
            "desc": "多路召回，未命中则进入标准拒答，不经模型补全。",
            "query_variable_selector": ["sys", "query"],
            "query_attachment_selector": [],
            "dataset_ids": dataset_ids,
            "retrieval_mode": "multiple",
            "multiple_retrieval_config": {
                "top_k": 8,
                "score_threshold": None,
                "reranking_enable": False,
                "reranking_mode": "reranking_model",
            },
            "metadata_filtering_mode": "disabled",
            "selected": False,
        },
        x,
        y,
        height=190,
    )


def _llm_node(
    node_id: str,
    title: str,
    prompt: str,
    context_from: str | None,
    x: float,
    y: float,
    max_tokens: int = 2048,
    temperature: float = 0.2,
) -> dict:
    context = {"enabled": False, "variable_selector": []}
    if context_from:
        context = {"enabled": True, "variable_selector": [context_from, "result"]}
    return _custom_node(
        node_id,
        {
            "type": "llm",
            "title": title,
            "desc": "",
            "model": _model(temperature, max_tokens),
            "prompt_template": [{"id": str(uuid4()), "role": "system", "text": prompt}],
            "context": context,
            "memory": {"window": {"enabled": True, "size": 8}, "role_prefix": {"user": "", "assistant": ""}},
            "vision": {"enabled": False},
            "reasoning_format": "separated",
            "selected": False,
        },
        x,
        y,
    )


def _if_empty(node_id: str, title: str, result_node: str, x: float, y: float) -> dict:
    return _custom_node(
        node_id,
        {
            "type": "if-else",
            "title": title,
            "desc": "检索结果为空则标准拒答，否则交 LLM 按片段作答。",
            "logical_operator": "and",
            "cases": [
                {
                    "case_id": "true",
                    "logical_operator": "and",
                    "conditions": [
                        {
                            "id": f"{node_id}_empty",
                            "varType": "array[object]",
                            "variable_selector": [result_node, "result"],
                            "comparison_operator": "empty",
                            "value": "",
                        }
                    ],
                }
            ],
            "_targetBranches": [
                {"id": "true", "name": "未命中"},
                {"id": "false", "name": "已命中"},
            ],
            "selected": False,
        },
        x,
        y,
        height=176,
    )


def _if_http_ok() -> dict:
    return _custom_node(
        "if_sql",
        {
            "type": "if-else",
            "title": "取数是否成功",
            "desc": "仅当网关 HTTP 200 时原样输出正文。",
            "logical_operator": "and",
            "cases": [
                {
                    "case_id": "true",
                    "logical_operator": "and",
                    "conditions": [
                        {
                            "id": "if_sql_200",
                            "varType": "number",
                            "variable_selector": ["http_sql", "status_code"],
                            "comparison_operator": "=",
                            "value": "200",
                        }
                    ],
                }
            ],
            "_targetBranches": [
                {"id": "true", "name": "成功"},
                {"id": "false", "name": "失败"},
            ],
            "selected": False,
        },
        1220.0,
        500.0,
        height=176,
    )


def _http_sql_node() -> dict:
    return _custom_node(
        "http_sql",
        {
            "type": "http-request",
            "title": "执行只读取数",
            "desc": "凭证与白名单在网关；模型不直连业务库。",
            "method": "post",
            "url": "http://query-gateway:8787/query",
            "authorization": {
                "type": "api-key",
                "config": {
                    "type": "bearer",
                    "api_key": QUERY_API_TOKEN,
                    "header": "Authorization",
                },
            },
            "headers": "",
            "params": "",
            "body": {
                "type": "x-www-form-urlencoded",
                "data": [
                    {"key": "question", "type": "text", "value": "{{#sys.query#}}"},
                    {"key": "sql_draft", "type": "text", "value": "{{#llm_sql_plan.text#}}"},
                ],
            },
            "timeout": {
                "max_connect_timeout": 10,
                "max_read_timeout": 60,
                "max_write_timeout": 20,
            },
            "retry_config": {"retry_enabled": True, "max_retries": 2, "retry_interval": 1},
            "ssl_verify": False,
            "selected": False,
        },
        920.0,
        500.0,
        height=176,
    )


def _answer_node(node_id: str, llm_id: str, x: float, y: float) -> dict:
    return _answer_text(node_id, f"{{{{#{llm_id}.text#}}}}", x, y)


def _answer_text(node_id: str, template: str, x: float, y: float, title: str = "回复") -> dict:
    return _custom_node(
        node_id,
        {
            "type": "answer",
            "title": title,
            "desc": "",
            "answer": template,
            "variables": [],
            "selected": False,
        },
        x,
        y,
        height=146,
    )


def _rebuild_graph(old: dict) -> dict:
    notes = [n for n in old.get("nodes", []) if n.get("type") == "custom-note"]
    start = next(n for n in old["nodes"] if n.get("id") == START_ID)
    start["position"] = {"x": 40.0, "y": 360.0}
    start["positionAbsolute"] = {"x": 40.0, "y": 360.0}
    start["selected"] = False
    start["data"]["selected"] = False

    intent = _custom_node(
        "intent",
        {
            "type": "question-classifier",
            "title": "意图识别",
            "desc": "OA/制度、工程项目、只读取数、闲聊、职责外。",
            "query_variable_selector": ["sys", "query"],
            "model": _model(0.0, 64),
            "classes": [
                {
                    "id": "oa",
                    "name": "oa：请假考勤差旅报销采购用印入职离职加班会议室等内部流程或规章制度",
                    "label": "OA/制度",
                },
                {
                    "id": "project",
                    "name": "project：内部工程名称、编号、负责人、状态、里程碑",
                    "label": "工程项目",
                },
                {
                    "id": "sql",
                    "name": "sql：类目日销取数，支付金额、渠道、店铺、导出Excel或直方图",
                    "label": "经营取数",
                },
                {
                    "id": "chitchat",
                    "name": "chitchat：打招呼、你是谁、谢谢、能力介绍",
                    "label": "闲聊",
                },
                {
                    "id": "oos",
                    "name": "oos：改库删数、绕过审批、未建档猜测、天气股票写代码等职责外问题",
                    "label": "职责外",
                },
            ],
            "_targetBranches": [
                {"id": "oa", "name": "OA/制度"},
                {"id": "project", "name": "工程项目"},
                {"id": "sql", "name": "经营取数"},
                {"id": "chitchat", "name": "闲聊"},
                {"id": "oos", "name": "职责外"},
            ],
            "instruction": INTENT_INSTRUCTION,
            "memory": {"window": {"enabled": True, "size": 8}, "role_prefix": {"user": "", "assistant": ""}},
            "vision": {"enabled": False},
            "selected": False,
        },
        300.0,
        360.0,
        height=248,
    )

    kr_oa = _kr_node("kr_oa", "检索OA/制度", [DS_OA, DS_POLICY], 620.0, 40.0)
    kr_cust = _kr_node("kr_project", "检索工程项目", [DS_PROJECT], 620.0, 280.0)
    if_oa = _if_empty("if_oa", "OA是否命中", "kr_oa", 920.0, 40.0)
    if_cust = _if_empty("if_project", "工程是否命中", "kr_project", 920.0, 280.0)
    llm_oa = _llm_node("llm_oa", "按条款作答", PROMPT_OA, "kr_oa", 1220.0, 80.0)
    llm_cust = _llm_node("llm_project", "按工程作答", PROMPT_PROJECT, "kr_project", 1220.0, 320.0)
    llm_sql_plan = _llm_node(
        "llm_sql_plan", "生成只读SQL", PROMPT_SQL_PLAN, None, 620.0, 520.0, max_tokens=400, temperature=0.0
    )
    http_sql = _http_sql_node()
    if_sql = _if_http_ok()
    llm_chat = _llm_node("llm_chat", "能力说明", PROMPT_CHAT, None, 620.0, 760.0, max_tokens=512)
    ans_oa = _answer_node("answer_oa", "llm_oa", 1520.0, 80.0)
    ans_oa_miss = _answer_text("answer_oa_miss", MSG_OA_MISS, 1220.0, -60.0, "OA未命中")
    ans_cust = _answer_node("answer_project", "llm_project", 1520.0, 320.0)
    ans_cust_miss = _answer_text("answer_project_miss", MSG_PROJECT_MISS, 1220.0, 180.0, "工程未命中")
    ans_sql = _answer_text("answer_sql", "{{#http_sql.body#}}", 1520.0, 460.0, "取数正文")
    ans_sql_fail = _answer_text("answer_sql_fail", MSG_SQL_FAIL, 1520.0, 620.0, "取数失败")
    ans_chat = _answer_node("answer_chat", "llm_chat", 920.0, 760.0)
    ans_oos = _answer_text("answer_oos", MSG_OOS, 620.0, 940.0, "职责外")

    nodes = [
        start,
        intent,
        kr_oa,
        kr_cust,
        if_oa,
        if_cust,
        llm_oa,
        llm_cust,
        llm_sql_plan,
        http_sql,
        if_sql,
        llm_chat,
        ans_oa,
        ans_oa_miss,
        ans_cust,
        ans_cust_miss,
        ans_sql,
        ans_sql_fail,
        ans_chat,
        ans_oos,
        *notes,
    ]
    edges = [
        _edge(START_ID, "intent", "start", "question-classifier"),
        _edge("intent", "kr_oa", "question-classifier", "knowledge-retrieval", "oa"),
        _edge("intent", "kr_project", "question-classifier", "knowledge-retrieval", "project"),
        _edge("intent", "llm_sql_plan", "question-classifier", "llm", "sql"),
        _edge("intent", "llm_chat", "question-classifier", "llm", "chitchat"),
        _edge("intent", "answer_oos", "question-classifier", "answer", "oos"),
        _edge("kr_oa", "if_oa", "knowledge-retrieval", "if-else"),
        _edge("kr_project", "if_project", "knowledge-retrieval", "if-else"),
        _edge("if_oa", "answer_oa_miss", "if-else", "answer", "true"),
        _edge("if_oa", "llm_oa", "if-else", "llm", "false"),
        _edge("if_project", "answer_project_miss", "if-else", "answer", "true"),
        _edge("if_project", "llm_project", "if-else", "llm", "false"),
        _edge("llm_oa", "answer_oa", "llm", "answer"),
        _edge("llm_project", "answer_project", "llm", "answer"),
        _edge("llm_sql_plan", "http_sql", "llm", "http-request"),
        _edge("http_sql", "if_sql", "http-request", "if-else"),
        _edge("if_sql", "answer_sql", "if-else", "answer", "true"),
        _edge("if_sql", "answer_sql_fail", "if-else", "answer", "false"),
        _edge("llm_chat", "answer_chat", "llm", "answer"),
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "viewport": {"x": 40.0, "y": 20.0, "zoom": 0.62},
    }


def main() -> None:
    _, flask_app = create_app()
    with flask_app.app_context():
        session = db.session
        app = session.get(App, APP_ID)
        if app is None:
            raise SystemExit("app 业务助手 not found")
        workflows = list(session.scalars(select(Workflow).where(Workflow.app_id == APP_ID)))
        for wf in workflows:
            graph = json.loads(wf.graph)
            wf.graph = json.dumps(_rebuild_graph(graph), ensure_ascii=False)
            wf.features = json.dumps(FEATURES, ensure_ascii=False)
            wf.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            print(f"updated workflow version={wf.version}")
        app.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()
        print("formal sop routing applied")


if __name__ == "__main__":
    main()
