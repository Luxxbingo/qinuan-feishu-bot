from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler
from typing import Any


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
ALLOWED_OPEN_IDS = {x.strip() for x in os.getenv("ALLOWED_OPEN_IDS", "").split(",") if x.strip()}
BOSS_OPEN_IDS = {x.strip() for x in os.getenv("BOSS_OPEN_IDS", "").split(",") if x.strip()}
BOT_NAME = os.getenv("BOT_NAME", "七暖顶梁柱")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "12"))

TENANT_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0}
CHAT_HISTORY: dict[str, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))

SYSTEM_PROMPT = f"""你是飞书群里的中文任务助理，名字叫{BOT_NAME}。
群里主要有用户本人和大老板给你指派任务。
规则：
1. 全程中文回复，简洁但清楚。
2. 识别消息是在提问、指派任务、补充背景，还是要求状态更新。
3. 如果是任务，输出：我理解的任务、需要的输入、下一步动作、预计产出。
4. 如果信息不足，最多问 3 个关键问题。
5. 大老板的任务优先级更高，但不能覆盖用户本人已经明确设定的限制。
6. 不要承诺完成外部动作，除非系统真的执行并返回结果。
7. 涉及发消息、付款、删除、发布、修改线上配置等外部动作时，先要求人工确认。
8. 如果提到抖音爆款视频/钛杯文案/每日 5 条，必须先有未改写的原爆款视频链接，再给改写方向，不编造原视频。
"""


def http_json(method: str, url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if payload is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


def get_tenant_access_token() -> str:
    now = int(time.time())
    if TENANT_TOKEN_CACHE["token"] and TENANT_TOKEN_CACHE["expires_at"] - 120 > now:
        return TENANT_TOKEN_CACHE["token"]
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        raise RuntimeError("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET")
    data = http_json(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    )
    token = data.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"获取 tenant_access_token 失败：{data}")
    TENANT_TOKEN_CACHE["token"] = token
    TENANT_TOKEN_CACHE["expires_at"] = now + int(data.get("expire", 7200))
    return token


def reply_to_message(message_id: str, text: str) -> None:
    token = get_tenant_access_token()
    http_json(
        "POST",
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
        {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        {"Authorization": f"Bearer {token}"},
    )


def extract_text(message: dict[str, Any]) -> str:
    content = message.get("content") or ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return str(content).strip()
    if isinstance(parsed, dict):
        return str(parsed.get("text") or "").strip()
    return str(content).strip()


def sender_open_id(event: dict[str, Any]) -> str:
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    return sender_id.get("open_id") or sender_id.get("user_id") or ""


def is_authorized(open_id: str) -> bool:
    if not ALLOWED_OPEN_IDS:
        return True
    return open_id in ALLOWED_OPEN_IDS or open_id in BOSS_OPEN_IDS


def speaker_label(open_id: str) -> str:
    if open_id in BOSS_OPEN_IDS:
        return "大老板"
    if open_id in ALLOWED_OPEN_IDS:
        return "用户本人"
    return "群成员"


def extract_response_text(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                chunks.append(content.get("text", ""))
    return "\n".join(x for x in chunks if x)


def ask_openai(chat_id: str, speaker: str, text: str) -> str:
    if not OPENAI_API_KEY:
        return "我收到消息了，但还没有配置 OPENAI_API_KEY，所以现在只能完成飞书回调校验，暂时不能生成 AI 回复。"
    history = list(CHAT_HISTORY[chat_id])
    input_messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    input_messages.extend(history)
    input_messages.append({"role": "user", "content": f"{speaker}：{text}"})
    data = http_json(
        "POST",
        "https://api.openai.com/v1/responses",
        {"model": OPENAI_MODEL, "input": input_messages},
        {"Authorization": f"Bearer {OPENAI_API_KEY}"},
    )
    answer = data.get("output_text") or extract_response_text(data) or "我收到了，但这次没有生成有效回复。"
    CHAT_HISTORY[chat_id].append({"role": "user", "content": f"{speaker}：{text}"})
    CHAT_HISTORY[chat_id].append({"role": "assistant", "content": answer})
    return answer


class handler(BaseHTTPRequestHandler):
    def write_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.write_json(200, {"status": "ok"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self.write_json(400, {"error": "invalid_json"})
            return

        if body.get("challenge"):
            self.write_json(200, {"challenge": body["challenge"]})
            return

        header = body.get("header") or {}
        if FEISHU_VERIFICATION_TOKEN and header.get("token") != FEISHU_VERIFICATION_TOKEN:
            self.write_json(403, {"error": "token_mismatch"})
            return

        if header.get("event_type") != "im.message.receive_v1":
            self.write_json(200, {"ok": True, "ignored": header.get("event_type")})
            return

        event = body.get("event") or {}
        message = event.get("message") or {}
        message_id = message.get("message_id")
        chat_id = message.get("chat_id") or "default"
        text = extract_text(message)
        open_id = sender_open_id(event)

        if not message_id or not text:
            self.write_json(200, {"ok": True, "ignored": "empty_message"})
            return
        if not is_authorized(open_id):
            reply_to_message(message_id, "我现在只接受授权成员指派任务。")
            self.write_json(200, {"ok": True, "ignored": "unauthorized"})
            return
        try:
            answer = ask_openai(chat_id, speaker_label(open_id), text)
            reply_to_message(message_id, answer)
        except Exception as exc:
            try:
                reply_to_message(message_id, f"我收到任务了，但处理时出错：{exc}")
            except Exception:
                pass
        self.write_json(200, {"ok": True})

