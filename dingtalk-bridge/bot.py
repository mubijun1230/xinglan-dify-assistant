#!/usr/bin/env python3
"""DingTalk Stream robot -> local Dify chat app."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Dict, Tuple
from urllib.parse import quote

import requests
from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage, CallbackMessage, Credential, DingTalkStreamClient

from error_i18n import translate_error

CLIENT_ID = os.environ["DINGTALK_CLIENT_ID"].strip()
CLIENT_SECRET = os.environ["DINGTALK_CLIENT_SECRET"].strip()
DIFY_OPEN_API_URL = os.environ.get("DIFY_OPEN_API_URL", "http://host.docker.internal/v1").rstrip("/")
DIFY_APP_API_KEY = os.environ["DIFY_APP_API_KEY"].strip()
CONVERSATION_TTL_SEC = int(os.environ.get("DIFY_CONVERSATION_TTL_MINUTES", "15")) * 60
REQUEST_TIMEOUT = int(os.environ.get("DIFY_REQUEST_TIMEOUT", "120"))
QUERY_GATEWAY_URL = os.environ.get("QUERY_GATEWAY_URL", "http://query-gateway:8787").rstrip("/")
QUERY_API_TOKEN = os.environ.get("QUERY_API_TOKEN", "").strip()
EXCEL_MARK = re.compile(r"\[\[DIFY_EXCEL:([^\]|]+)\|([^\]|]+)\|([^\]]+)\]\]")
CHART_MARK = re.compile(r"\[\[DIFY_CHART:([^\]|]+)\|([^\]|]+)\|([^\]]+)\]\]")


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("dify-dingtalk")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(message)s [%(filename)s:%(lineno)d]")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def get_access_token() -> str:
    resp = requests.get(
        "https://oapi.dingtalk.com/gettoken",
        params={"appkey": CLIENT_ID, "appsecret": CLIENT_SECRET},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"钉钉 token 失败: {data}")
    return token


def upload_media(token: str, content: bytes, filename: str, media_type: str = "file") -> str:
    lower = filename.lower()
    if lower.endswith(".png"):
        mime = "image/png"
    elif lower.endswith(".xlsx"):
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        mime = "application/octet-stream"
    resp = requests.post(
        "https://oapi.dingtalk.com/media/upload",
        params={"access_token": token, "type": media_type},
        files={"media": (filename, content, mime)},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    media_id = data.get("media_id") or data.get("mediaId")
    if not media_id:
        raise RuntimeError(f"钉钉上传文件失败: {data}")
    return media_id if str(media_id).startswith("@") else f"@{media_id}"


class DifyChatBotHandler(ChatbotHandler):
    def __init__(self, logger: logging.Logger):
        super(ChatbotHandler, self).__init__()
        self.logger = logger
        self._sessions: Dict[str, Tuple[str, float]] = {}

    def _session_key(self, incoming: ChatbotMessage) -> str:
        sender = incoming.sender_staff_id or incoming.sender_nick or "unknown"
        conv = incoming.conversation_id or "default"
        return f"{conv}:{sender}"

    def _conversation_id(self, key: str) -> str:
        now = time.time()
        rec = self._sessions.get(key)
        if rec and now - rec[1] < CONVERSATION_TTL_SEC:
            return rec[0]
        self._sessions.pop(key, None)
        return ""

    def _remember(self, key: str, conversation_id: str) -> None:
        if conversation_id:
            self._sessions[key] = (conversation_id, time.time())

    def _ask_dify(self, query: str, user: str, conversation_id: str) -> tuple[str, str]:
        payload = {
            "inputs": {},
            "query": query,
            "response_mode": "blocking",
            "user": user,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        resp = requests.post(
            f"{DIFY_OPEN_API_URL}/chat-messages",
            headers={
                "Authorization": f"Bearer {DIFY_APP_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            self.logger.error("Dify HTTP %s: %s", resp.status_code, resp.text[:800])
            raise RuntimeError(translate_error(resp.text[:800]))
        data = resp.json()
        answer = (data.get("answer") or "").strip() or "(Dify 没有返回文本)"
        return answer, data.get("conversation_id") or conversation_id

    def _download_export(self, export_id: str, token: str) -> bytes:
        url = f"{QUERY_GATEWAY_URL}/exports/{export_id}?t={quote(token)}"
        headers = {}
        if QUERY_API_TOKEN:
            headers["Authorization"] = f"Bearer {QUERY_API_TOKEN}"
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.content

    def _send_file(self, incoming: ChatbotMessage, content: bytes, filename: str) -> None:
        token = get_access_token()
        ext = (filename.rsplit(".", 1)[-1] if "." in filename else "xlsx").lower()
        media_id = upload_media(token, content, filename, media_type="file")
        webhook_payload = {
            "msgtype": "file",
            "file": {"mediaId": media_id, "fileName": filename, "fileType": ext},
        }
        webhook_resp = requests.post(incoming.session_webhook, json=webhook_payload, timeout=20)
        if webhook_resp.ok and webhook_resp.json().get("errcode", 0) == 0:
            return
        self.logger.warning("webhook file failed: %s", webhook_resp.text[:400])
        headers = {
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        }
        msg_param = json.dumps(
            {"mediaId": media_id, "fileName": filename, "fileType": ext},
            ensure_ascii=False,
        )
        conv_type = str(incoming.conversation_type or "")
        if conv_type == "1":
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            body = {
                "robotCode": incoming.robot_code or CLIENT_ID,
                "userIds": [incoming.sender_staff_id],
                "msgKey": "sampleFile",
                "msgParam": msg_param,
            }
        else:
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            body = {
                "robotCode": incoming.robot_code or CLIENT_ID,
                "openConversationId": incoming.conversation_id,
                "msgKey": "sampleFile",
                "msgParam": msg_param,
            }
        api_resp = requests.post(url, headers=headers, json=body, timeout=20)
        if not api_resp.ok:
            raise RuntimeError(f"钉钉发文件失败: {api_resp.text[:400]}")

    async def process(self, callback: CallbackMessage):
        incoming = ChatbotMessage.from_dict(callback.data)
        query = (incoming.text.content if incoming.text else "") or ""
        query = query.strip()
        if not query:
            self.reply_text("请发送文字消息。", incoming)
            return AckMessage.STATUS_OK, "OK"

        key = self._session_key(incoming)
        user = f"dingtalk-{incoming.sender_staff_id or 'user'}"
        conv_id = self._conversation_id(key)
        self.logger.info("query from %s: %s", user, query[:200])
        try:
            answer, new_conv = self._ask_dify(query, user, conv_id)
            self._remember(key, new_conv)
        except Exception as exc:
            self.logger.exception("Dify request failed")
            self.reply_text(translate_error(exc), incoming)
            return AckMessage.STATUS_OK, "OK"

        excel = EXCEL_MARK.search(answer)
        if excel:
            export_id, file_token, filename = excel.group(1), excel.group(2), excel.group(3)
            answer = EXCEL_MARK.sub("", answer).strip()
            try:
                content = self._download_export(export_id, file_token)
                self._send_file(incoming, content, filename)
            except Exception:
                self.logger.exception("send excel failed")
                answer = answer + "\n（Excel 发送失败，请稍后重试）"

        chart = CHART_MARK.search(answer)
        if chart:
            export_id, file_token, filename = chart.group(1), chart.group(2), chart.group(3)
            answer = CHART_MARK.sub("", answer).strip()
            try:
                content = self._download_export(export_id, file_token)
                self._send_file(incoming, content, filename)
            except Exception:
                self.logger.exception("send chart failed")
                answer = answer + "\n（直方图发送失败，请稍后重试）"

        if len(answer) > 18000:
            answer = answer[:18000] + "\n…(已截断)"
        self.reply_text(answer, incoming)
        return AckMessage.STATUS_OK, "OK"


def main() -> None:
    logger = setup_logger()
    if not DIFY_APP_API_KEY or DIFY_APP_API_KEY.startswith("REPLACE"):
        raise SystemExit("请先在 dingtalk-bridge/.env 里填写 DIFY_APP_API_KEY")
    logger.info("connecting Stream client_id=%s dify=%s", CLIENT_ID, DIFY_OPEN_API_URL)
    credential = Credential(CLIENT_ID, CLIENT_SECRET)
    client = DingTalkStreamClient(credential)
    client.register_callback_handler(ChatbotMessage.TOPIC, DifyChatBotHandler(logger))
    client.start_forever()


if __name__ == "__main__":
    main()
