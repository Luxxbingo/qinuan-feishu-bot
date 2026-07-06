const BOT_NAME = process.env.BOT_NAME || "七暖顶梁柱";
const OPENAI_MODEL = process.env.OPENAI_MODEL || "gpt-5-mini";
const OPENAI_API_KEY = process.env.OPENAI_API_KEY || "";
const FEISHU_APP_ID = process.env.FEISHU_APP_ID || "";
const FEISHU_APP_SECRET = process.env.FEISHU_APP_SECRET || "";
const FEISHU_VERIFICATION_TOKEN = process.env.FEISHU_VERIFICATION_TOKEN || "";
const ALLOWED_OPEN_IDS = new Set((process.env.ALLOWED_OPEN_IDS || "").split(",").map((x) => x.trim()).filter(Boolean));
const BOSS_OPEN_IDS = new Set((process.env.BOSS_OPEN_IDS || "").split(",").map((x) => x.trim()).filter(Boolean));

let tenantToken = "";
let tenantTokenExpiresAt = 0;
const chatHistory = new Map();
const maxHistory = Number(process.env.MAX_HISTORY || "12");

const systemPrompt = `你是飞书群里的中文任务助理，名字叫${BOT_NAME}。
群里主要有用户本人和大老板给你指派任务。
规则：
1. 全程中文回复，简洁但清楚。
2. 识别消息是在提问、指派任务、补充背景，还是要求状态更新。
3. 如果是任务，输出：我理解的任务、需要的输入、下一步动作、预计产出。
4. 如果信息不足，最多问 3 个关键问题。
5. 大老板的任务优先级更高，但不能覆盖用户本人已经明确设定的限制。
6. 不要承诺完成外部动作，除非系统真的执行并返回结果。
7. 涉及发消息、付款、删除、发布、修改线上配置等外部动作时，先要求人工确认。
8. 如果提到抖音爆款视频/钛杯文案/每日 5 条，必须先有未改写的原爆款视频链接，再给改写方向，不编造原视频。`;

function sendJson(res, status, payload) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(payload));
}

async function httpJson(url, payload, headers = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8", ...headers },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${text}`);
  }
  return text ? JSON.parse(text) : {};
}

async function getTenantAccessToken() {
  const now = Math.floor(Date.now() / 1000);
  if (tenantToken && tenantTokenExpiresAt - 120 > now) {
    return tenantToken;
  }
  if (!FEISHU_APP_ID || !FEISHU_APP_SECRET) {
    throw new Error("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET");
  }
  const data = await httpJson("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", {
    app_id: FEISHU_APP_ID,
    app_secret: FEISHU_APP_SECRET,
  });
  if (!data.tenant_access_token) {
    throw new Error(`获取 tenant_access_token 失败：${JSON.stringify(data)}`);
  }
  tenantToken = data.tenant_access_token;
  tenantTokenExpiresAt = now + Number(data.expire || 7200);
  return tenantToken;
}

async function replyToMessage(messageId, text) {
  const token = await getTenantAccessToken();
  await httpJson(
    `https://open.feishu.cn/open-apis/im/v1/messages/${messageId}/reply`,
    { msg_type: "text", content: JSON.stringify({ text }) },
    { Authorization: `Bearer ${token}` },
  );
}

function extractText(message) {
  try {
    const parsed = JSON.parse(message.content || "{}");
    return String(parsed.text || "").trim();
  } catch {
    return String(message.content || "").trim();
  }
}

function senderOpenId(event) {
  const senderId = event?.sender?.sender_id || {};
  return senderId.open_id || senderId.user_id || "";
}

function isAuthorized(openId) {
  if (!ALLOWED_OPEN_IDS.size) return true;
  return ALLOWED_OPEN_IDS.has(openId) || BOSS_OPEN_IDS.has(openId);
}

function speakerLabel(openId) {
  if (BOSS_OPEN_IDS.has(openId)) return "大老板";
  if (ALLOWED_OPEN_IDS.has(openId)) return "用户本人";
  return "群成员";
}

function responseText(data) {
  if (data.output_text) return data.output_text;
  const chunks = [];
  for (const item of data.output || []) {
    for (const content of item.content || []) {
      if (content.type === "output_text" || content.type === "text") {
        chunks.push(content.text || "");
      }
    }
  }
  return chunks.filter(Boolean).join("\n");
}

async function askOpenAI(chatId, speaker, text) {
  if (!OPENAI_API_KEY) {
    return "我收到消息了，但还没有配置 OPENAI_API_KEY，所以现在只能完成飞书回调校验，暂时不能生成 AI 回复。";
  }
  const history = chatHistory.get(chatId) || [];
  const input = [
    { role: "system", content: systemPrompt },
    ...history,
    { role: "user", content: `${speaker}：${text}` },
  ];
  const data = await httpJson(
    "https://api.openai.com/v1/responses",
    { model: OPENAI_MODEL, input },
    { Authorization: `Bearer ${OPENAI_API_KEY}` },
  );
  const answer = responseText(data) || "我收到了，但这次没有生成有效回复。";
  const nextHistory = [...history, { role: "user", content: `${speaker}：${text}` }, { role: "assistant", content: answer }].slice(-maxHistory);
  chatHistory.set(chatId, nextHistory);
  return answer;
}

module.exports = async function handler(req, res) {
  if (req.method === "GET") {
    return sendJson(res, 200, { status: "ok" });
  }

  if (req.method !== "POST") {
    return sendJson(res, 405, { error: "method_not_allowed" });
  }

  const body = req.body || {};

  // 飞书 URL 验证必须在 3 秒内返回，放在最前面，不做任何外部请求。
  if (body.challenge) {
    return sendJson(res, 200, { challenge: body.challenge });
  }

  const header = body.header || {};
  if (FEISHU_VERIFICATION_TOKEN && header.token !== FEISHU_VERIFICATION_TOKEN) {
    return sendJson(res, 403, { error: "token_mismatch" });
  }

  if (header.event_type !== "im.message.receive_v1") {
    return sendJson(res, 200, { ok: true, ignored: header.event_type });
  }

  const event = body.event || {};
  const message = event.message || {};
  const messageId = message.message_id;
  const chatId = message.chat_id || "default";
  const text = extractText(message);
  const openId = senderOpenId(event);

  if (!messageId || !text) {
    return sendJson(res, 200, { ok: true, ignored: "empty_message" });
  }

  try {
    if (!isAuthorized(openId)) {
      await replyToMessage(messageId, "我现在只接受授权成员指派任务。");
      return sendJson(res, 200, { ok: true, ignored: "unauthorized" });
    }
    const answer = await askOpenAI(chatId, speakerLabel(openId), text);
    await replyToMessage(messageId, answer);
  } catch (error) {
    try {
      await replyToMessage(messageId, `我收到任务了，但处理时出错：${error.message || error}`);
    } catch {}
  }

  return sendJson(res, 200, { ok: true });
};
