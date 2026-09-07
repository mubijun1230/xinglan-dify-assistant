"""把模型/Agent 原始报错转成可操作的中文说明。"""

from __future__ import annotations

import json
import re

_AGENT_RUN_ID = re.compile(r"\s*\(agent_run_id=([^)]+)\)\s*$")

# 每条规则：关键词组（全部命中，或其中一条足够长的短语命中）→ 中文说明
_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("20015", "system message must be at the beginning"),
        "智谱 GLM 拒绝了这次请求：系统提示（system）必须放在对话最前面。\n"
        "常见原因：智能体（Agent）会在历史消息后面再插入系统说明，智谱不允许这种顺序。\n"
        "处理建议：1）Agent 场景改用硅基流动等对消息顺序更宽松的模型；"
        "2）继续用 GLM 时改用对话/工作流 LLM 节点，只保留一条系统提示并放在最前；"
        "3）改完后请新开一轮对话。",
    ),
    (
        ("system message must be at the beginning",),
        "智谱 GLM 拒绝了这次请求：系统提示（system）必须放在对话最前面。\n"
        "处理建议：Agent 请换其他模型；或改用 LLM 节点并只保留一条系统提示，然后新开对话。",
    ),
    (
        ("messages 参数非法",),
        "智谱认为消息格式不合法（错误码 1214）。\n"
        "处理建议：把系统提示挪到用户提示开头，不要使用智谱不支持的消息角色，并新开一轮对话。",
    ),
    (
        ("输入不能为空",),
        "智谱返回「输入不能为空」。通常是系统提示被拒收后，实际发给模型的内容变成了空。\n"
        "处理建议：把说明写进用户提示，不要单独使用 system；改完后新开对话。",
    ),
    (
        ("incorrect api key",),
        "模型密钥无效或未配置。\n处理建议：打开「设置 → 模型供应商」，检查 API Key 是否填对、是否过期。",
    ),
    (
        ("invalid api key",),
        "模型密钥无效。\n处理建议：打开「设置 → 模型供应商」，更换正确的 API Key。",
    ),
    (
        ("provider_not_initialize",),
        "还没有配置这个模型供应商。\n处理建议：打开「设置 → 模型供应商」，完成授权后再试。",
    ),
    (
        ("quota for dify hosted model",),
        "Dify 托管模型额度已用完。\n处理建议：打开「设置 → 模型供应商」，改用你自己的供应商密钥。",
    ),
    (
        ("余额不足",),
        "模型余额不足。\n处理建议：到对应供应商控制台充值，或更换可用密钥。",
    ),
    (
        ("insufficient_quota", "insufficient quota"),
        "模型额度不足。\n处理建议：到对应供应商控制台充值或更换密钥。",
    ),
    (
        ("rate limit",),
        "请求过于频繁，触发了模型限流。\n处理建议：稍等片刻再试，或降低并发、换额度更高的密钥。",
    ),
    (
        ("too many requests",),
        "请求过于频繁。\n处理建议：稍等片刻再试。",
    ),
    (
        ("context_length_exceeded", "maximum context length", "context length"),
        "输入内容超出模型上下文长度。\n处理建议：缩短提示词、减少对话轮次、减少知识库召回，或换更长上下文的模型。",
    ),
    (
        ("timed out", "timeout"),
        "调用模型超时。\n处理建议：稍后重试；若经常超时，检查网络/代理，或换更快的模型。",
    ),
    (
        ("content_filter", "content filter"),
        "内容被模型安全策略拦截。\n处理建议：调整提问或知识库内容后重试。",
    ),
    (
        ("model_not_found", "does not exist", "invalid model"),
        "指定的模型不存在或当前密钥无权使用。\n处理建议：在应用编排里更换模型，或到供应商开通该模型。",
    ),
    (
        ("internal server error, please contact support",),
        "服务内部出错。\n处理建议：稍后重试；若反复出现，查看 Docker 中 api 容器日志。",
    ),
]


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _extract_json_blob(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _haystack(text: str) -> str:
    blob = _extract_json_blob(text)
    extra = ""
    if blob:
        extra = " ".join(str(blob.get(k, "")) for k in ("code", "message", "error", "msg"))
        nested = blob.get("error")
        if isinstance(nested, dict):
            extra += " " + " ".join(str(nested.get(k, "")) for k in ("code", "message", "type"))
    return f"{text} {extra}".lower()


def _matched(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(n.lower() in haystack for n in needles)


def translate_error(raw: object) -> str:
    """把任意异常/字符串转成中文说明；无法识别时保留原文并加引导。"""
    text = _normalize(str(raw or ""))
    if not text:
        return "调用失败，但没有返回具体原因。请稍后重试；若反复出现，查看 Docker 中 api 容器日志。"

    run_id = None
    matched = _AGENT_RUN_ID.search(text)
    if matched:
        run_id = matched.group(1)
        text = text[: matched.start()].rstrip()

    haystack = _haystack(text)
    translated = next((msg for needles, msg in _RULES if _matched(haystack, needles)), None)

    if translated is None:
        translated = (
            "调用模型失败。\n"
            f"原始信息：{text}\n"
            "可先根据原文判断（密钥、额度、限流、超时、消息格式），或把这段原文发给管理员协助排查。"
        )

    if run_id:
        translated = f"{translated}\n排查编号：{run_id}"
    return translated
