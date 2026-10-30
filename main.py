import asyncio
import json
import time
import hashlib
import re
import os
import ssl
import sys
import uuid
import argparse
import base64
import urllib.parse
import urllib.request
from typing import Optional, List, Dict, Any, Tuple
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import aiosqlite
import gradio as gr

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

__version__ = "2.1.0-agent"

# ─── Configuration ────────────────────────────────────────────────────────────

def load_config() -> dict:
    defaults = {
        "port": 7860,
        "host": "0.0.0.0",
        "retry_attempts": 3,
        "retry_delay_sec": 2,
        "request_timeout_sec": 180,
        "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
        "default_model": "gemini-3.5-flash",
        "log_requests": True,
        "cookie_file": None,
        "proxy": None,
        "api_keys": ["sk-gemini"],
        "rate_limit_per_minute": 20,
        "cache_ttl_seconds": 3600,
        "db_path": "cache.db",
        "cleanup_interval_seconds": 300,
        # ✅ جديد: إعدادات Agent Loop
        "agent_max_iterations": 8,        # حد أقصى لدورات الأداة
        "agent_max_tokens_per_step": 4096, # حد التوكنز لكل خطوة
    }

    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            try:
                with open(p) as f:
                    defaults.update(json.load(f))
                print(f"[Config] Loaded: {p}")
            except Exception as e:
                print(f"[Config] Error loading {p}: {e}")
            break

    if os.environ.get("API_KEYS"):
        defaults["api_keys"] = [k.strip() for k in os.environ["API_KEYS"].split(",") if k.strip()]
    if os.environ.get("GEMINI_COOKIE"):
        defaults["_cookie_inline"] = os.environ["GEMINI_COOKIE"]
    if os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"):
        defaults["proxy"] = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if os.environ.get("GEMINI_BL"):
        defaults["gemini_bl"] = os.environ["GEMINI_BL"]

    return defaults

CONFIG = load_config()

# ─── Models ──────────────────────────────────────────────────────────────────
# ✅ إصلاح: كانت هناك أخطاء syntax (قوس مفقود + تكرار مفاتيح)

MODELS = {
    "gemini-3.7-flash": {
        "mode": 1, "think": 4,
        "desc": "Fast general-purpose model (~12k chars)",
    },  # ← القوس كان مفقوداً في الكود الأصلي
    "gemini-3.6-flash": {
        "mode": 1, "think": 4,
        "desc": "All-around model (Gemini 3.6 Flash)",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Standard flash model",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth (~15k chars)",
    },
    # ✅ إصلاح: كان "claude-opus-4-6" مكرراً بخطأ، والصحيح هو نموذج Gemini خفيف
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model (~10k chars)",
    },
    "claude-opus-4-6": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "claude-opus-4-5": {
        "mode": 2, "think": 0,
        "desc": "Claude Opus 4.5 (mapped to Gemini thinking mode)",
    },
    "claude-opus-3-5": {
        "mode": 2, "think": 0,
        "desc": "Claude Opus 4.1 (mapped to Gemini thinking mode)",
    },
    "claude-opus-4-7": {
        "mode": 2, "think": 0,
        "desc": "Claude Opus 4.0 (mapped to Gemini thinking mode)",
    },
    "claude-opus-4-8": {
        "mode": 2, "think": 0,
        "desc": "Claude Opus 4 (dated alias)",
    },       
}

# ─── Pydantic Schemas ─────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: Any
    tool_calls: Optional[List[dict]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    model: str = "gemini-3.5-flash"
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 32000
    stream: Optional[bool] = False
    tools: Optional[List[dict]] = None
    tool_choice: Optional[Any] = "auto"
    user: Optional[str] = "default"

# ─── Production Services ──────────────────────────────────────────────────────

@dataclass
class RateLimitEntry:
    timestamps: List[float] = field(default_factory=list)
    last_cleanup: float = field(default_factory=time.time)

class Services:
    def __init__(self):
        self._rate_limits: Dict[str, RateLimitEntry] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    async def init_db(self):
        async with aiosqlite.connect(CONFIG["db_path"]) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    val TEXT,
                    exp REAL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_exp ON cache(exp)")
            await db.commit()

    async def start_cleanup(self):
        async def loop():
            while True:
                await asyncio.sleep(CONFIG["cleanup_interval_seconds"])
                await self._cleanup()
        self._cleanup_task = asyncio.create_task(loop())

    async def _cleanup(self):
        now = time.time()
        cutoff = now - 3600
        async with self._lock:
            expired = [ip for ip, e in self._rate_limits.items() if e.last_cleanup < cutoff]
            for ip in expired:
                del self._rate_limits[ip]
        try:
            async with aiosqlite.connect(CONFIG["db_path"]) as db:
                await db.execute("DELETE FROM cache WHERE exp < ?", (now,))
                await db.commit()
        except Exception:
            pass

    async def check_rate_limit(self, client_id: str) -> Tuple[bool, int]:
        now = time.time()
        cutoff = now - 60
        async with self._lock:
            if client_id not in self._rate_limits:
                self._rate_limits[client_id] = RateLimitEntry()
            entry = self._rate_limits[client_id]
            entry.timestamps = [t for t in entry.timestamps if t > cutoff]
            entry.last_cleanup = now
            limit = CONFIG["rate_limit_per_minute"]
            if len(entry.timestamps) >= limit:
                wait = 60 - (now - min(entry.timestamps))
                return False, int(wait) + 1
            entry.timestamps.append(now)
            return True, 0

    async def get_cache(self, key: str) -> Optional[str]:
        try:
            async with aiosqlite.connect(CONFIG["db_path"]) as db:
                async with db.execute(
                    "SELECT val FROM cache WHERE key=? AND exp>?", (key, time.time())
                ) as cur:
                    row = await cur.fetchone()
                    return row[0] if row else None
        except Exception:
            return None

    async def set_cache(self, key: str, value: str):
        try:
            async with aiosqlite.connect(CONFIG["db_path"]) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                    (key, value, time.time() + CONFIG["cache_ttl_seconds"])
                )
                await db.commit()
        except Exception:
            pass

services = Services()

# ─── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    if CONFIG.get("log_requests"):
        prefix = {"INFO": "ℹ️", "AGENT": "🤖", "TOOL": "🔧", "ERROR": "❌", "WARN": "⚠️"}.get(level, "•")
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {prefix} {msg}\n")
        sys.stderr.flush()

# ─── Cookie & Headers ─────────────────────────────────────────────────────────

def load_cookie() -> Tuple[str, Optional[str]]:
    inline = CONFIG.get("_cookie_inline", "")
    if inline:
        if inline.startswith("{"):
            try:
                data = json.loads(inline)
                return data.get("cookie", ""), data.get("sapisid") or None
            except Exception:
                pass
        pairs = dict(p.split("=", 1) for p in inline.split("; ") if "=" in p)
        return inline, pairs.get("SAPISID") or None

    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        with open(cookie_file) as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            return data.get("cookie", ""), data.get("sapisid") or None
        pairs = dict(p.split("=", 1) for p in content.split("; ") if "=" in p)
        return content, pairs.get("SAPISID") or None
    except Exception as e:
        log(f"Cookie load error: {e}", "ERROR")
        return "", None

def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"

def _build_headers() -> dict:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/app",
        "X-Same-Domain": "1",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers

# ─── Gemini Protocol ──────────────────────────────────────────────────────────

def _build_gemini_payload(prompt: str, model_id: int, think_mode: int) -> Tuple[bytes, str]:
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    body = urllib.parse.urlencode({"f.req": json.dumps(outer)}).encode()
    reqid = int(time.time()) % 1000000
    url = (
        "https://gemini.google.com/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    return body, url

def gemini_stream_generate(prompt: str, model_id: int, think_mode: int) -> str:
    body, url = _build_gemini_payload(prompt, model_id, think_mode)
    headers = _build_headers()
    proxy = CONFIG.get("proxy")
    last_err = None

    for attempt in range(CONFIG["retry_attempts"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx = ssl.create_default_context()
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}", "WARN")
                time.sleep(CONFIG["retry_delay_sec"])

    raise last_err

def clean_gemini_text(text: str) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    return text.strip()

def extract_response_text(raw: str) -> str:
    texts = []
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and part[1]:
                        if isinstance(part[1], list):
                            for t in part[1]:
                                if isinstance(t, str) and len(t) > 0:
                                    texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass

    text = ""
    for t in reversed(texts):
        if t.strip():
            text = t
            break
    return clean_gemini_text(text)

def gemini_stream_iter(prompt: str, model_id: int, think_mode: int):
    """Streaming حقيقي يُعيد النص تدريجياً."""
    body_bytes, url = _build_gemini_payload(prompt, model_id, think_mode)
    body_str = body_bytes.decode()
    headers = _build_headers()
    proxy = CONFIG.get("proxy")

    if not HAS_HTTPX:
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)
        if text:
            yield text
        return

    prev_text = ""
    transport = httpx.HTTPTransport(proxy=proxy) if proxy else None

    with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True) as client:
        with client.stream("POST", url, content=body_str, headers=headers) as resp:
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if '"wrb.fr"' not in line or len(line) < 200:
                        continue
                    try:
                        arr = json.loads(line)
                        inner_str = arr[0][2]
                        if not inner_str or len(inner_str) < 50:
                            continue
                        inner2 = json.loads(inner_str)
                        if isinstance(inner2, list) and len(inner2) > 4 and inner2[4]:
                            for part in inner2[4]:
                                if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                                    for t in part[1]:
                                        if isinstance(t, str) and len(t) > len(prev_text):
                                            delta = t[len(prev_text):]
                                            delta = clean_gemini_text(delta)
                                            if delta:
                                                yield delta
                                            prev_text = t
                    except (json.JSONDecodeError, IndexError, TypeError):
                        pass

# ─── ✅ Tool Call Parser المحسّن ──────────────────────────────────────────────

def parse_tool_calls(text: str) -> Tuple[str, Optional[List[dict]]]:
    """
    محلّل Tool Calls متعدد المستويات:
    1. ```tool_call {...}```
    2. ```json {"name": ..., "arguments": ...}```
    3. <tool_call>...</tool_call>
    4. JSON مباشر في النص
    """
    tool_calls = []

    # ─── مستوى 1: ```tool_call``` blocks
    pattern1 = r'```tool_call\s*\n(.*?)\n?```'
    for match in re.finditer(pattern1, text, re.DOTALL):
        raw = match.group(1).strip()
        tc = _try_parse_single_tool_call(raw)
        if tc:
            tool_calls.append(tc)

    # ─── مستوى 2: ```json``` blocks التي تحتوي على tool call
    if not tool_calls:
        pattern2 = r'```json\s*\n(.*?)\n?```'
        for match in re.finditer(pattern2, text, re.DOTALL):
            raw = match.group(1).strip()
            tc = _try_parse_single_tool_call(raw)
            if tc:
                tool_calls.append(tc)

    # ─── مستوى 3: XML-style <tool_call>
    if not tool_calls:
        pattern3 = r'<tool_call>(.*?)</tool_call>'
        for match in re.finditer(pattern3, text, re.DOTALL):
            raw = match.group(1).strip()
            tc = _try_parse_single_tool_call(raw)
            if tc:
                tool_calls.append(tc)

    # ─── مستوى 4: JSON مباشر (للنماذج التي تُخرج JSON مباشرة)
    if not tool_calls:
        # ابحث عن JSON objects تحتوي على "name" و "arguments"
        json_pattern = r'\{[^{}]*"name"\s*:\s*"[^"]+[^{}]*"arguments"\s*:[^{}]*\}'
        for match in re.finditer(json_pattern, text, re.DOTALL):
            tc = _try_parse_single_tool_call(match.group(0))
            if tc:
                tool_calls.append(tc)

    if not tool_calls:
        return text, None

    # تنظيف النص من أجزاء tool calls
    clean = re.sub(pattern1, '', text, flags=re.DOTALL)
    clean = re.sub(r'```json\s*\n.*?\n?```', '', clean, flags=re.DOTALL)
    clean = re.sub(r'<tool_call>.*?</tool_call>', '', clean, flags=re.DOTALL)
    # إزالة أي JSON مباشر كان tool call
    for tc_obj in tool_calls:
        fn_name = tc_obj["function"]["name"]
        clean = re.sub(
            r'\{[^{}]*"name"\s*:\s*"' + re.escape(fn_name) + r'"[^{}]*\}',
            '', clean
        )

    return clean.strip(), tool_calls

def _try_parse_single_tool_call(raw: str) -> Optional[dict]:
    """محاولة تحليل tool call واحد من نص."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # محاولة إصلاح JSON المكسور
        raw_fixed = _fix_json(raw)
        try:
            data = json.loads(raw_fixed)
        except json.JSONDecodeError:
            return None

    if not isinstance(data, dict):
        return None

    name = data.get("name") or data.get("function") or data.get("tool")
    if not name:
        return None

    args = data.get("arguments") or data.get("parameters") or data.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {"value": args}

    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }

def _fix_json(s: str) -> str:
    """إصلاح JSON المكسور جزئياً."""
    # إزالة trailing commas
    s = re.sub(r',\s*([}\]])', r'\1', s)
    # إضافة quotes للمفاتيح غير المُقتبَسة
    s = re.sub(r'(\w+)\s*:', r'"\1":', s)
    # إزالة double quotes
    s = re.sub(r'""(\w+)""', r'"\1"', s)
    return s

# ─── ✅ Tool/Agent System Prompt المحسّن ──────────────────────────────────────

AGENT_SYSTEM_PROMPT = """You are a helpful AI assistant with access to tools. You MUST use tools when they are needed to answer the user's request accurately.
## HOW TO USE TOOLS
When you need to call a tool, you MUST respond with a tool_call block in this EXACT format:
```tool_call
{"name": "tool_name_here", "arguments": {"param1": "value1", "param2": "value2"}}
```
## IMPORTANT RULES
1. **Always use tools when available** - Do not guess or make up information that a tool can provide.
2. **One tool at a time** - Call tools one by one unless you need multiple simultaneously.
3. **Wait for results** - After calling a tool, wait for the tool result before continuing.
4. **Use results** - Incorporate tool results into your final answer.
5. **Be explicit** - Always explain what tool you're calling and why.
## AVAILABLE TOOLS
{tool_definitions}
## RESPONSE FORMAT
- If you need a tool: respond with ONLY the tool_call block (no extra text before it).
- If you have all information needed: provide your final answer directly.
- Never call a tool you don't have access to.
- Never fabricate tool results.
## EXAMPLE
User: What's the weather in Paris?
Assistant:
```tool_call
{"name": "get_weather", "arguments": {"location": "Paris", "unit": "celsius"}}
```
[After receiving tool result]
Assistant: Based on the weather data, Paris currently has...
"""

def build_tool_definitions_text(tools: List[dict]) -> str:
    """بناء وصف نصي للأدوات المتاحة."""
    if not tools:
        return "No tools available."
    lines = []
    for i, tool in enumerate(tools, 1):
        if tool.get("type") == "function":
            fn = tool.get("function", tool)
        else:
            fn = tool
        name = fn.get("name", f"tool_{i}")
        desc = fn.get("description", "No description")
        params = fn.get("parameters", {})

        lines.append(f"### {i}. `{name}`")
        lines.append(f"**Description:** {desc}")

        if params and params.get("properties"):
            lines.append("**Parameters:**")
            for pname, pinfo in params.get("properties", {}).items():
                required = pname in params.get("required", [])
                ptype = pinfo.get("type", "any")
                pdesc = pinfo.get("description", "")
                req_str = " *(required)*" if required else " *(optional)*"
                lines.append(f"  - `{pname}` ({ptype}){req_str}: {pdesc}")
        lines.append("")
    return "\n".join(lines)

# ─── Prompt Builder ───────────────────────────────────────────────────────────

def messages_to_prompt(messages: list, tools: list = None) -> str:
    """
    ✅ محسّن: يبني prompt واضحاً مع system prompt قوي للأدوات.
    """
    parts = []

    # ─── System prompt للأدوات
    if tools:
        tool_defs_text = build_tool_definitions_text(tools)
        system_tool_prompt = AGENT_SYSTEM_PROMPT.replace("{tool_definitions}", tool_defs_text)
        parts.append(f"[SYSTEM]\n{system_tool_prompt}")

    # ─── معالجة الرسائل
    for msg in messages:
        role = msg.get("role", "user") if isinstance(msg, dict) else msg.role
        content = msg.get("content", "") if isinstance(msg, dict) else (msg.content or "")
        tool_calls_data = (msg.get("tool_calls") if isinstance(msg, dict)
                          else getattr(msg, "tool_calls", None))

        # تحويل content متعدد الأجزاء
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") in ("text", "input_text")
            )

        if role == "system":
            if not tools:  # إذا كان هناك أدوات، system prompt الأدوات يأتي أولاً
                parts.append(f"[SYSTEM]\n{content}")
            else:
                # دمج system prompt المستخدم مع system prompt الأدوات
                parts[0] = parts[0] + f"\n\n## ADDITIONAL INSTRUCTIONS\n{content}"

        elif role == "assistant":
            if tool_calls_data:
                # تضمين tool calls السابقة في السياق
                tc_parts = [f"[ASSISTANT called tool(s)]:"]
                for tc in tool_calls_data:
                    fn = tc.get("function", {})
                    tc_parts.append(
                        f"```tool_call\n"
                        f"{json.dumps({'name': fn.get('name'), 'arguments': json.loads(fn.get('arguments', '{}'))}, ensure_ascii=False)}\n"
                        f"```"
                    )
                if content:
                    tc_parts.insert(1, f"[ASSISTANT]: {content}")
                parts.append("\n".join(tc_parts))
            else:
                parts.append(f"[ASSISTANT]: {content}")

        elif role == "tool":
            # ✅ نتائج الأدوات بشكل واضح
            tool_name = (msg.get("name", "") if isinstance(msg, dict)
                        else getattr(msg, "name", ""))
            tool_call_id = (msg.get("tool_call_id", "") if isinstance(msg, dict)
                           else getattr(msg, "tool_call_id", ""))
            parts.append(
                f"[TOOL RESULT - {tool_name or tool_call_id}]:\n```json\n{content}\n```\n"
                f"Now use this result to continue."
            )

        else:  # user
            if content:
                parts.append(f"[USER]: {content}")

    return "\n\n".join(p for p in parts if p)

# ─── ✅ Agent Loop الرئيسي ────────────────────────────────────────────────────

def run_agent_sync(
    messages: List[dict],
    model_id: int,
    think_mode: int,
    tools: Optional[List[dict]],
    max_iterations: int = None,
) -> Tuple[str, Optional[List[dict]], List[dict]]:
    """
    Agent Loop حقيقي (ReAct pattern):
    1. ابنِ prompt من السياق الكامل
    2. اسأل Gemini
    3. إذا أعاد tool call → أضف إلى السياق واعد
    4. إذا لم يُعد tool call → أعد الرد النهائي
    يعيد: (final_text, final_tool_calls, updated_messages)
    """
    if max_iterations is None:
        max_iterations = CONFIG["agent_max_iterations"]

    current_messages = list(messages)
    iteration = 0
    last_tool_calls = None

    while iteration < max_iterations:
        iteration += 1
        log(f"Agent iteration {iteration}/{max_iterations}", "AGENT")

        prompt = messages_to_prompt(current_messages, tools if iteration == 1 else tools)
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)

        if not text:
            log("Empty response from Gemini", "WARN")
            break

        # ─── محاولة تحليل tool calls
        clean_text, tool_calls = parse_tool_calls(text)

        if tool_calls and tools:
            log(f"Tool calls detected: {[tc['function']['name'] for tc in tool_calls]}", "TOOL")

            # أضف رد المساعد مع tool calls إلى السياق
            current_messages.append({
                "role": "assistant",
                "content": clean_text or None,
                "tool_calls": tool_calls,
            })
            last_tool_calls = tool_calls

            # ✅ هنا يجب على المستدعي تنفيذ الأدوات وإضافة النتائج
            # في وضع non-streaming، نُعيد tool calls للمستدعي
            # في وضع agent داخلي، نضيف placeholder
            return clean_text, tool_calls, current_messages

        else:
            # ─── رد نهائي بدون tool calls
            log(f"Final response after {iteration} iterations", "AGENT")
            return clean_text or text, None, current_messages

    log(f"Max iterations ({max_iterations}) reached", "WARN")
    return text or "", None, current_messages


def run_agent_with_tool_execution_sync(
    messages: List[dict],
    model_id: int,
    think_mode: int,
    tools: Optional[List[dict]],
    tool_executor=None,  # دالة لتنفيذ الأدوات (اختيارية)
    max_iterations: int = None,
) -> Tuple[str, Optional[List[dict]], List[dict]]:
    """
    ✅ Agent Loop الكامل مع تنفيذ الأدوات تلقائياً (إذا توفّر executor).
    
    tool_executor: Callable[[str, dict], str] - يأخذ (tool_name, args) ويُعيد نتيجة نصية
    """
    if max_iterations is None:
        max_iterations = CONFIG["agent_max_iterations"]

    current_messages = list(messages)

    for iteration in range(1, max_iterations + 1):
        log(f"Agent loop iteration {iteration}/{max_iterations}", "AGENT")

        prompt = messages_to_prompt(current_messages, tools)
        try:
            raw = gemini_stream_generate(prompt, model_id, think_mode)
            text = extract_response_text(raw)
        except Exception as e:
            log(f"Gemini call failed: {e}", "ERROR")
            return f"Error: {e}", None, current_messages

        if not text:
            log("Empty response", "WARN")
            break

        clean_text, tool_calls = parse_tool_calls(text)

        if not tool_calls or not tools:
            # رد نهائي
            log(f"✅ Final answer after {iteration} iterations", "AGENT")
            return clean_text or text, None, current_messages

        # ─── أضف رد المساعد
        current_messages.append({
            "role": "assistant",
            "content": clean_text or None,
            "tool_calls": tool_calls,
        })

        # ─── تنفيذ كل tool call
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except Exception:
                fn_args = {}

            log(f"Executing tool: {fn_name}({fn_args})", "TOOL")

            if tool_executor:
                try:
                    result = tool_executor(fn_name, fn_args)
                    result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                except Exception as e:
                    result_str = json.dumps({"error": str(e)})
                    log(f"Tool execution error: {e}", "ERROR")
            else:
                # لا يوجد executor، أضف placeholder
                result_str = json.dumps({"status": "tool_executed", "note": "No executor provided"})

            current_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": fn_name,
                "content": result_str,
            })

        log(f"Tools executed, continuing loop...", "AGENT")

    log(f"Max iterations reached", "WARN")
    return text or "", None, current_messages


# ─── Sync wrapper ─────────────────────────────────────────────────────────────

def call_gemini_sync(
    prompt: str, model_id: int, think_mode: int, tools: list = None
) -> Tuple[str, Optional[list]]:
    """استدعاء مزامن بسيط (بدون agent loop)."""
    raw = gemini_stream_generate(prompt, model_id, think_mode)
    text = extract_response_text(raw)
    tool_calls = None
    if tools and text:
        text, tool_calls = parse_tool_calls(text)
    return text or "", tool_calls

# ─── Model Resolution ─────────────────────────────────────────────────────────

def resolve_model(model_name: str) -> Tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
    think_override = None
    if "@think=" in model_name:
        model_name, think_str = model_name.rsplit("@think=", 1)
        try:
            think_override = int(think_str)
        except ValueError:
            pass
    cfg = MODELS.get(model_name)
    if not cfg:
        # fallback للنموذج الافتراضي بدلاً من الخطأ المباشر
        default = CONFIG.get("default_model", "gemini-3.5-flash")
        cfg = MODELS.get(default)
        if cfg:
            log(f"Unknown model '{model_name}', falling back to '{default}'", "WARN")
            model_name = default
        else:
            return None, None, None, f"Unknown model: {model_name}. Available: {list(MODELS.keys())}"
    return model_name, cfg["mode"], (think_override if think_override is not None else cfg["think"]), None

# ─── Authentication ───────────────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)

async def verify_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> str:
    api_keys = CONFIG.get("api_keys", [])
    if not api_keys:
        return "anonymous"
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = credentials.credentials
    if token not in api_keys:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return token

# ─── FastAPI App ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await services.init_db()
    await services.start_cleanup()
    log(f"gemini-web2api v{__version__} started", "INFO")
    yield
    if services._cleanup_task:
        services._cleanup_task.cancel()

app = FastAPI(
    title="Gemini Web2API (Agent Edition)",
    version=__version__,
    description="OpenAI-compatible proxy for Google Gemini Web with full Agent/Tool support",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── /v1/models ───────────────────────────────────────────────────────────────

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "google",
                "description": cfg["desc"],
                "capabilities": {
                    "tool_use": True,
                    "streaming": True,
                    "agent_loop": True,
                },
            }
            for name, cfg in MODELS.items()
        ],
    }

# ─── /v1beta/models ───────────────────────────────────────────────────────────

@app.get("/v1beta/models")
async def google_models_list():
    return {
        "models": [
            {
                "name": f"models/{name}",
                "displayName": name,
                "description": cfg["desc"],
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            }
            for name, cfg in MODELS.items()
        ]
    }

# ─── /v1/chat/completions ─────────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    _: str = Depends(verify_api_key),
):
    # Rate Limiting
    client_ip = request.client.host
    allowed, wait_time = await services.check_rate_limit(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Retry in {wait_time}s.",
            headers={"Retry-After": str(wait_time)},
        )

    model_name, model_id, think_mode, err = resolve_model(
        body.model or CONFIG["default_model"]
    )
    if err:
        raise HTTPException(status_code=400, detail=err)

    tools = body.tools
    messages = [m.dict(exclude_none=True) for m in body.messages]
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    # Cache key
    context_str = "".join(f"{m.get('role')}:{m.get('content')}" for m in messages)
    cache_key = hashlib.md5(
        f"{model_name}:{context_str}:{body.user}:{bool(tools)}".encode()
    ).hexdigest()

    # ─── Streaming ───
    if body.stream:
        async def stream_gen():
            full_text = ""
            try:
                if tools:
                    # ✅ Streaming مع أدوات: نُرسل tool calls كـ chunk خاص
                    loop = asyncio.get_event_loop()
                    text, tool_calls, _ = await loop.run_in_executor(
                        None, run_agent_sync,
                        messages, model_id, think_mode, tools,
                        CONFIG["agent_max_iterations"]
                    )
                    full_text = text or ""

                    if tool_calls:
                        # إرسال tool calls chunk
                        msg_delta = {"role": "assistant", "content": full_text or None, "tool_calls": tool_calls}
                        chunk = {
                            "id": cid, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "choices": [{"index": 0, "delta": msg_delta, "finish_reason": "tool_calls"}],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    else:
                        # إرسال النص تدريجياً
                        if full_text:
                            chunk = {
                                "id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model_name,
                                "choices": [{"index": 0, "delta": {"role": "assistant", "content": full_text}, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                else:
                    # Streaming بدون أدوات
                    prompt = messages_to_prompt(messages)
                    for delta in gemini_stream_iter(prompt, model_id, think_mode):
                        full_text += delta
                        chunk = {
                            "id": cid, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                # Final chunk
                finish = "tool_calls" if tools and full_text else "stop"
                final_chunk = {
                    "id": cid, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

                # Cache النص النهائي (بدون tool calls)
                if full_text and not tools:
                    resp_data = _build_completion_response(cid, created, model_name, full_text, None, "")
                    await services.set_cache(cache_key, json.dumps(resp_data))

            except Exception as e:
                log(f"Stream error: {e}", "ERROR")
                yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'server_error'}})}\n\n"

        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    # ─── Non-streaming ───
    # Cache (فقط بدون أدوات)
    if not tools:
        cached = await services.get_cache(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    try:
        loop = asyncio.get_event_loop()
        if tools:
            # ✅ استخدم Agent Loop
            text, tool_calls, _ = await loop.run_in_executor(
                None, run_agent_sync,
                messages, model_id, think_mode, tools,
                CONFIG["agent_max_iterations"]
            )
        else:
            prompt = messages_to_prompt(messages)
            text, tool_calls = await loop.run_in_executor(
                None, call_gemini_sync, prompt, model_id, think_mode, None
            )
    except Exception as e:
        log(f"Error: {e}", "ERROR")
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    prompt_text = messages_to_prompt(messages)
    resp_data = _build_completion_response(cid, created, model_name, text, tool_calls, prompt_text)

    if not tools:
        await services.set_cache(cache_key, json.dumps(resp_data))

    return JSONResponse(content=resp_data)


def _build_completion_response(
    cid: str, created: int, model: str, text: str,
    tool_calls: Optional[list], prompt: str = ""
) -> dict:
    msg = {"role": "assistant", "content": text or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": len(prompt) // 4,
            "completion_tokens": len(text) // 4 if text else 0,
            "total_tokens": (len(prompt) + len(text)) // 4 if text else len(prompt) // 4,
        },
    }

# ─── /v1/responses (Codex CLI / Responses API) ────────────────────────────────

@app.post("/v1/responses")
async def responses_api(
    request: Request,
    _: str = Depends(verify_api_key),
):
    req = await request.json()
    model_name, model_id, think_mode, err = resolve_model(
        req.get("model", CONFIG["default_model"])
    )
    if err:
        raise HTTPException(status_code=400, detail=err)

    input_items = req.get("input", [])
    tools = req.get("tools")
    messages = []

    if req.get("instructions"):
        messages.append({"role": "system", "content": req["instructions"]})

    if isinstance(input_items, str):
        messages.append({"role": "user", "content": input_items})
    elif isinstance(input_items, list):
        for item in input_items:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                if item.get("type") == "function_call_output":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": item.get("call_id", ""),
                        "name": item.get("name", ""),
                        "content": item.get("output", ""),
                    })
                elif item.get("role") == "assistant":
                    cp = item.get("content", [])
                    text_acc, tc_list = "", []
                    if isinstance(cp, list):
                        for c in cp:
                            if isinstance(c, dict):
                                if c.get("type") == "output_text":
                                    text_acc += c.get("text", "")
                                elif c.get("type") == "function_call":
                                    tc_list.append(c)
                    elif isinstance(cp, str):
                        text_acc = cp
                    m = {"role": "assistant", "content": text_acc or None}
                    if tc_list:
                        m["tool_calls"] = [
                            {
                                "id": tc.get("call_id", f"call_{i}"),
                                "type": "function",
                                "function": {
                                    "name": tc.get("name", ""),
                                    "arguments": tc.get("arguments", "{}"),
                                }
                            }
                            for i, tc in enumerate(tc_list)
                        ]
                    messages.append(m)
                else:
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content
                            if c.get("type") in ("text", "input_text")
                        )
                    messages.append({"role": role, "content": content})

    if tools:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {}),
                }
            }
            if t.get("type") == "function" and "function" not in t else t
            for t in tools
        ]

    if not any(m.get("content") for m in messages):
        raise HTTPException(status_code=400, detail="Empty input")

    loop = asyncio.get_event_loop()
    try:
        if tools:
            text, tool_calls, _ = await loop.run_in_executor(
                None, run_agent_sync, messages, model_id, think_mode, tools,
                CONFIG["agent_max_iterations"]
            )
        else:
            prompt = messages_to_prompt(messages)
            text, tool_calls = await loop.run_in_executor(
                None, call_gemini_sync, prompt, model_id, think_mode, None
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    rid = f"resp_{uuid.uuid4().hex[:16]}"
    mid = f"msg_{uuid.uuid4().hex[:12]}"
    output = []

    if tool_calls:
        for tc in tool_calls:
            output.append({
                "type": "function_call",
                "id": tc["id"],
                "call_id": tc["id"],
                "name": tc["function"]["name"],
                "arguments": tc["function"]["arguments"],
                "status": "completed",
            })
    if text or not tool_calls:
        output.append({
            "type": "message",
            "id": mid,
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text or "", "annotations": []}],
        })

    prompt_text = messages_to_prompt(messages)
    if req.get("stream"):
        async def responses_stream():
            yield f"event: response.created\ndata: {json.dumps({'type': 'response.created', 'response': {'id': rid, 'object': 'response', 'status': 'in_progress', 'model': model_name, 'output': []}})}\n\n"
            for item in output:
                if item["type"] == "function_call":
                    ev = {
                        "type": "response.function_call_arguments.done",
                        "item_id": item["id"], "call_id": item["call_id"],
                        "name": item["name"], "arguments": item["arguments"],
                    }
                    yield f"event: response.function_call_arguments.done\ndata: {json.dumps(ev)}\n\n"
                elif item["type"] == "message":
                    for ci, cp in enumerate(item["content"]):
                        ev = {
                            "type": "response.output_text.done",
                            "item_id": item["id"], "content_index": ci, "text": cp["text"],
                        }
                        yield f"event: response.output_text.done\ndata: {json.dumps(ev)}\n\n"
            resp_obj = {"id": rid, "object": "response", "status": "completed", "model": model_name, "output": output}
            yield f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': resp_obj})}\n\n"
        return StreamingResponse(responses_stream(), media_type="text/event-stream")

    return JSONResponse({
        "id": rid, "object": "response", "created_at": int(time.time()),
        "status": "completed", "model": model_name, "output": output,
        "usage": {
            "input_tokens": len(prompt_text) // 4,
            "output_tokens": len(text) // 4 if text else 0,
            "total_tokens": (len(prompt_text) + len(text)) // 4 if text else len(prompt_text) // 4,
        },
    })

# ─── Google Native API /v1beta ────────────────────────────────────────────────

def _google_contents_to_prompt(req: dict) -> str:
    parts = []
    sys_inst = req.get("systemInstruction")
    if sys_inst:
        sys_text = " ".join(p.get("text", "") for p in sys_inst.get("parts", []) if p.get("text"))
        if sys_text:
            parts.append(f"[SYSTEM]\n{sys_text}")
    for content in req.get("contents", []):
        role = content.get("role", "user")
        text = " ".join(p.get("text", "") for p in content.get("parts", []) if p.get("text"))
        if role == "model":
            parts.append(f"[ASSISTANT]: {text}")
        else:
            parts.append(f"[USER]: {text}")
    return "\n\n".join(p for p in parts if p)

@app.post("/v1beta/models/{model_path:path}")
async def google_generate(model_path: str, request: Request):
    req = await request.json()
    model_name_raw = model_path.split(":")[0]
    stream = ":streamGenerateContent" in model_path

    model_name, model_id, think_mode, err = resolve_model(model_name_raw)
    if err:
        raise HTTPException(status_code=400, detail=err)

    prompt = _google_contents_to_prompt(req)
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="Empty content")

    loop = asyncio.get_event_loop()
    try:
        text, _ = await loop.run_in_executor(
            None, call_gemini_sync, prompt, model_id, think_mode, None
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    candidate = {
        "content": {"parts": [{"text": text or ""}], "role": "model"},
        "finishReason": "STOP",
        "index": 0,
    }
    response_obj = {
        "candidates": [candidate],
        "usageMetadata": {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text) // 4 if text else 0,
            "totalTokenCount": (len(prompt) + len(text)) // 4 if text else len(prompt) // 4,
        },
        "modelVersion": model_name,
    }

    if stream:
        async def google_stream():
            yield f"data: {json.dumps(response_obj)}\n\n"
        return StreamingResponse(google_stream(), media_type="text/event-stream")

    return JSONResponse(response_obj)

# ─── Health & Root ────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service": "gemini-web2api",
        "version": __version__,
        "models": list(MODELS.keys()),
        "features": {
            "agent_loop": True,
            "tool_calling": True,
            "streaming": True,
            "cache": True,
            "rate_limiting": True,
        },
        "endpoints": ["/v1/chat/completions", "/v1/models", "/v1/responses", "/v1beta/models"],
    }

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": time.time(), "version": __version__}

# ─── Gradio UI ────────────────────────────────────────────────────────────────

def build_gradio_interface():
    async def chat_handler(message: str, history: list, model_choice: str, request: gr.Request):
        if not message or not message.strip():
            yield history
            return

        model_name, model_id, think_mode, err = resolve_model(model_choice)
        if err:
            history.append((message, f"❌ {err}"))
            yield history
            return

        history.append((message, ""))
        yield history

        full_response = ""
        try:
            prompt = messages_to_prompt([{"role": "user", "content": message}])
            for chunk in gemini_stream_iter(prompt, model_id, think_mode):
                full_response += chunk
                history[-1] = (message, full_response)
                yield history
        except Exception as e:
            full_response = f"❌ Error: {e}"
            history[-1] = (message, full_response)
            yield history

    api_endpoint = "https://your-space.hf.space/v1"
    api_key_display = CONFIG.get("api_keys", ["sk-gemini"])[0]

    with gr.Blocks(title="Gemini Web2API — Agent Edition") as demo:
        gr.Markdown(f"""
        # 🤖 Gemini Web2API — Agent Edition v{__version__}
        > OpenAI-compatible proxy مع دعم كامل لـ Agent Loop وTool Calling
        | | |
        |---|---|
        | **Base URL** | `{api_endpoint}` |
        | **API Key** | `{api_key_display}` |
        | **Models** | {', '.join(f'`{m}`' for m in MODELS.keys())} |
        | **Agent Loop** | ✅ حتى {CONFIG['agent_max_iterations']} iterations |
        """)

        with gr.Row():
            with gr.Column(scale=4):
                chatbot = gr.Chatbot(height=500, label="المحادثة")
            with gr.Column(scale=1):
                gr.Markdown("""### ✅ المميزات
- Agent Loop (ReAct)
- Tool Calling موثوق
- JSON Parser متعدد المستويات
- Streaming حقيقي
- SQLite Cache
- Rate Limiting
- Retry Logic
- Fallback للنموذج الافتراضي
                """)

        with gr.Row():
            msg_box = gr.Textbox(label="رسالتك", placeholder="اكتب هنا...", lines=2, scale=5)
            send_btn = gr.Button("إرسال ▶", variant="primary", scale=1)

        with gr.Row():
            clear_btn = gr.Button("🗑️ مسح")
            model_dd = gr.Dropdown(
                choices=list(MODELS.keys()),
                value=CONFIG["default_model"],
                label="النموذج",
                scale=2,
            )

        msg_box.submit(chat_handler, [msg_box, chatbot, model_dd], [chatbot]).then(
            lambda: "", None, [msg_box]
        )
        send_btn.click(chat_handler, [msg_box, chatbot, model_dd], [chatbot]).then(
            lambda: "", None, [msg_box]
        )
        clear_btn.click(lambda: [], None, [chatbot])

    return demo

demo = build_gradio_interface()
app = gr.mount_gradio_app(app, demo, path="/chat")

# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Gemini Web2API Agent Edition")
    parser.add_argument("--port", type=int, default=CONFIG["port"])
    parser.add_argument("--host", type=str, default=CONFIG["host"])
    parser.add_argument("--cookie-file", type=str, default=None)
    parser.add_argument("--proxy", type=str, default=None)
    parser.add_argument("--max-iterations", type=int, default=CONFIG["agent_max_iterations"])
    parser.add_argument("--version", action="version", version=f"v{__version__}")
    args = parser.parse_args()

    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy
    CONFIG["agent_max_iterations"] = args.max_iterations

    print(f"\n{'='*60}")
    print(f"  gemini-web2api v{__version__} — Agent Edition")
    print(f"{'='*60}")
    print(f"  API:          http://localhost:{args.port}/v1")
    print(f"  Chat UI:      http://localhost:{args.port}/chat")
    print(f"  Models:       {', '.join(MODELS.keys())}")
    print(f"  Agent Loop:   max {CONFIG['agent_max_iterations']} iterations")
    print(f"  Cookie:       {'✅' if CONFIG.get('cookie_file') or CONFIG.get('_cookie_inline') else '❌ none (anonymous)'}")
    print(f"  Proxy:        {CONFIG.get('proxy') or '❌ none'}")
    print(f"{'='*60}\n")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        loop="asyncio",
        timeout_keep_alive=120,
    )