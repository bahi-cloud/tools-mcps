# tools.py
# جميع الأدوات المحلية مع دعم اللغة العربية
# ★ نسخة متكاملة مع main.py + hub.py (عزل مستخدمين كامل)
# ★ التعديلات الجراحية:
#   1) _save_bytes_and_url يحوي client_id في file_url
#   2) كل دوال الإنشاء تقبل client_id
#   3) read_pdf و fetch_media_to_file محذوفة (في main.py بحماية SSRF)
#   4) create_pdf محذوفة (في main.py مع _arabic_font_ready)
#   5) _safe_filename محسّن لمنع path traversal

import os
import io
import json
import base64
import uuid
import asyncio
import httpx
from typing import Dict, Any, Callable, Optional, List
from dataclasses import dataclass, field
from pathlib import Path
from fastapi import Request

# ══════════════════════════════════════════════════════════
# ★ قراءة متغيرات البيئة (كسولة)
# ══════════════════════════════════════════════════════════

def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        val = os.environ.get(name)
        if val and val.strip():
            return val.strip()
    return default

_COMPOSIO_KEY_ALIASES = (
    "COMPOSIO_API_KEY",
    "COMPOSIO_KEY",
    "COMPOSIO_TOKEN",
    "COMPOSIO_API_TOKEN",
)

def _get_composio_key(api_key: Optional[str] = None) -> Optional[str]:
    if api_key and str(api_key).strip():
        return str(api_key).strip()
    return _env(*_COMPOSIO_KEY_ALIASES)

def _get_composio_base_url() -> str:
    return _env(
        "COMPOSIO_BASE_URL",
        "COMPOSIO_API_URL",
        default="https://backend.composio.dev/api/v3"
    )

def _get_composio_default_user_id() -> str:
    return _env("COMPOSIO_USER_ID", default="default")


# ══════════════════════════════════════════════════════════
# ★ دوال مساعدة — عزل الملفات
# ══════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent


def _safe_client_name(client_id: str) -> str:
    """
    ★ نفس نسخة main.py و hub.py تماماً — لضمان تطابق أسماء المجلدات.
    ASCII فقط، حد 64 حرفاً، وإلا hash احتياطي.
    """
    import hashlib
    cid = (client_id or "").strip()
    if not cid:
        return "anonymous"
    clean = "".join(c for c in cid if c.isascii() and (c.isalnum() or c in "-_"))
    if clean != cid or len(clean) > 64:
        return "c_" + hashlib.sha256(cid.encode("utf-8")).hexdigest()[:32]
    return clean


def _safe_filename(name: str, default_ext: str) -> str:
    """
    ★ ينظف اسم الملف ويضمن الامتداد الصحيح.
    - يمنع path traversal (لا / ولا \\ ولا ..)
    - يحد الطول بـ 60 حرفاً
    """
    if not name or not name.strip():
        return ""
    # احذف أي أحرف غير آمنة (بما فيها / و \)
    safe = "".join(c for c in name.strip() if c.isalnum() or c in "-_.")[:60]
    # احذف البادئات الخطرة
    safe = safe.lstrip(".-")
    if not safe or safe in (".", ".."):
        return ""
    if not safe.lower().endswith("." + default_ext):
        safe += "." + default_ext
    return safe


def _save_bytes_and_url(
    data: bytes,
    ext: str,
    request: Optional[Request],
    files_dir: Optional[Path],
    filename: str = "",
    client_id: str = "",
) -> dict:
    """
    ★ دالة موحدة لحفظ الملفات مع عزل client_id.
    - files_dir يُمرَّر من hub.py (files/<safe_client>/)
    - file_url يحوي client_id (متوافق مع /files/{client_id}/{filename})
    - relative_path يُعاد أيضاً للاستخدام في _sign_path
    """
    if files_dir is None:
        # احتياطي: استخدم files/<client_id>/ إذا لم يُمرَّر files_dir
        safe_client_fallback = _safe_client_name(client_id)
        files_dir = Path.cwd() / "files" / safe_client_fallback

    files_dir.mkdir(parents=True, exist_ok=True)

    safe = _safe_filename(filename, ext)
    if safe:
        path = files_dir / safe
        path.write_bytes(data)
        fname = safe
    else:
        fname = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
        path = files_dir / fname
        path.write_bytes(data)

    # ★ إصلاح: client_id في الرابط
    safe_client = _safe_client_name(client_id)
    rel = f"/files/{safe_client}/{fname}"

    if request:
        base_url = str(request.base_url).rstrip("/")
        file_url = f"{base_url}{rel}"
    else:
        file_url = rel

    return {
        "local_path": str(path),
        "relative_path": rel,
        "file_url": file_url,
    }


def _apply_markdown_lines(add_heading, add_paragraph, add_bullet, text: str):
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("### "):
            add_heading(line[4:], 3)
        elif line.startswith("## "):
            add_heading(line[3:], 2)
        elif line.startswith("# "):
            add_heading(line[2:], 1)
        elif line.startswith(("- ", "* ")):
            add_bullet(line[2:])
        else:
            add_paragraph(line)


def _pick_content(*values) -> str:
    for v in values:
        if v and isinstance(v, str) and v.strip():
            return v
    return ""


# ══════════════════════════════════════════════════════════
# 1. تعريف بنية الأداة
# ══════════════════════════════════════════════════════════

@dataclass
class LocalTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    callable: Callable
    category: str = "General"
    aliases: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    enabled: bool = True
    hidden: bool = False


# ══════════════════════════════════════════════════════════
# 2. Decorator
# ══════════════════════════════════════════════════════════

TOOLS: Dict[str, LocalTool] = {}


def tool(
    *,
    name: Optional[str] = None,
    description: str,
    parameters: Dict[str, Any],
    category: str = "General",
    aliases: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    enabled: bool = True,
    hidden: bool = False,
) -> Callable:
    def wrapper(fn: Callable) -> Callable:
        tool_name = name or fn.__name__
        TOOLS[tool_name] = LocalTool(
            name=tool_name,
            description=description,
            parameters=parameters,
            callable=fn,
            category=category,
            aliases=aliases or [],
            tags=tags or [],
            enabled=enabled,
            hidden=hidden,
        )
        return fn
    return wrapper


# ══════════════════════════════════════════════════════════
# 3. Firecrawl
# ══════════════════════════════════════════════════════════

async def firecrawl_search(query: str, limit: int = 5, api_key: Optional[str] = None) -> dict:
    api_key = api_key or _env("FIRECRAWL_API_KEY")
    if not api_key:
        return {"error": "FIRECRAWL_API_KEY غير مضبوط"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.post(
            "https://api.firecrawl.dev/v1/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": query, "limit": limit},
        )
        if r.status_code != 200:
            try:
                err_msg = r.json().get("error", str(r.json()))
            except Exception:
                err_msg = r.text[:500]
            return {"error": f"Firecrawl search error ({r.status_code}): {err_msg}"}
        data = r.json()
        results = data.get("data") or data.get("results") or []
        out = []
        for item in results[:limit]:
            out.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("description") or (item.get("markdown") or "")[:300],
            })
        return {"results": out}


async def firecrawl_scrape(url: str, api_key: Optional[str] = None) -> dict:
    api_key = api_key or _env("FIRECRAWL_API_KEY")
    if not api_key:
        return {"error": "FIRECRAWL_API_KEY غير مضبوط"}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
        )
        if r.status_code != 200:
            try:
                err_msg = r.json().get("error", str(r.json()))
            except Exception:
                err_msg = r.text[:500]
            return {"error": f"Firecrawl scrape error ({r.status_code}): {err_msg}"}
        data = r.json()
        markdown = (data.get("data") or {}).get("markdown", "")
        return {"markdown": markdown[:8000]}


# ══════════════════════════════════════════════════════════
# 4. Context7
# ══════════════════════════════════════════════════════════

async def context7_resolve_library(query: str, api_key: Optional[str] = None) -> dict:
    api_key = api_key or _env("CONTEXT7_API_KEY")
    if not api_key:
        return {"error": "CONTEXT7_API_KEY غير مضبوط"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            "https://context7.com/api/v2/libs/search",
            params={"libraryName": query, "query": query},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if r.status_code != 200:
            return {"error": f"Context7 API error ({r.status_code}): {r.text[:500]}"}
        data = r.json()
        results = data.get("results", data if isinstance(data, list) else [])
        libs = [{"id": it.get("id"), "title": it.get("title") or it.get("name")} for it in results[:8]]
        return {"libraries": libs}


async def context7_get_docs(library_id: str, api_key: Optional[str] = None, topic: Optional[str] = None) -> dict:
    api_key = api_key or _env("CONTEXT7_API_KEY")
    if not api_key:
        return {"error": "CONTEXT7_API_KEY غير مضبوط"}
    if library_id.startswith("/"):
        library_id = library_id[1:]
    params = {
        "libraryId": f"/{library_id}",
        "query": topic or "documentation",
        "type": "txt",
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            "https://context7.com/api/v2/context",
            params=params,
            headers={"Authorization": f"Bearer {api_key}", "X-Context7-Source": "mcp-server"},
        )
        if r.status_code != 200:
            return {"error": f"Context7 API error ({r.status_code}): {r.text[:500]}"}
        text = r.text
        return {"docs": text[:6000]}


# ══════════════════════════════════════════════════════════
# 5. Composio
# ══════════════════════════════════════════════════════════

async def composio_list_connected_tools(api_key: Optional[str] = None) -> dict:
    api_key = _get_composio_key(api_key)
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{_get_composio_base_url()}/connected_accounts",
            headers={"x-api-key": api_key},
        )
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح أو منتهي. تحقق من COMPOSIO_API_KEY."}
        if r.status_code != 200:
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        data = r.json()
        items = data.get("items", [])
        tools = [{
            "slug": (it.get("toolkit") or {}).get("slug"),
            "id": it.get("id"),
            "alias": it.get("alias"),
            "status": it.get("status"),
        } for it in items]
        return {"connected_tools": tools}


async def composio_list_tools(toolkit_slug: str, api_key: Optional[str] = None, limit: int = 20) -> dict:
    api_key = _get_composio_key(api_key)
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{_get_composio_base_url()}/tools",
            params={"toolkit_slug": toolkit_slug, "toolkit_versions": "latest", "limit": limit},
            headers={"x-api-key": api_key},
        )
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code != 200:
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        data = r.json()
        items = data.get("items", data if isinstance(data, list) else [])
        tools = [{
            "slug": it.get("slug") or it.get("name"),
            "description": (it.get("description") or "")[:200],
        } for it in items[:limit]]
        return {"toolkit": toolkit_slug, "tools": tools}


async def composio_execute_tool(
    slug: str,
    api_key: Optional[str] = None,
    arguments: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    version: str = "latest",
) -> dict:
    api_key = _get_composio_key(api_key)
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    payload = {
        "arguments": arguments or {},
        "version": version,
        "user_id": user_id or _get_composio_default_user_id(),
    }
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.post(
            f"{_get_composio_base_url()}/tools/execute/{slug}",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 404:
            return {"error": f"الأداة '{slug}' غير موجودة (404)."}
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code not in (200, 201):
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        return r.json()


async def composio_revoke_account(connected_account_id: str, api_key: Optional[str] = None) -> dict:
    api_key = _get_composio_key(api_key)
    if not api_key:
        return {"error": "COMPOSIO_API_KEY غير مضبوط على الخادم"}
    if not connected_account_id or not connected_account_id.strip():
        return {
            "error": "المعامل 'connected_account_id' مطلوب.",
            "suggestion": "استدعِ composio_list_connected_tools أولاً.",
        }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.post(
            f"{_get_composio_base_url()}/connected_accounts/{connected_account_id}/revoke",
            headers={"x-api-key": api_key},
        )
        if r.status_code == 404:
            return {
                "error": f"الحساب '{connected_account_id}' غير موجود.",
                "status_code": 404,
                "suggestion": "تحقق من الـ id عبر composio_list_connected_tools.",
            }
        if r.status_code == 401:
            return {"error": "مفتاح Composio API غير صالح."}
        if r.status_code not in (200, 201, 204):
            return {"error": f"Composio API error ({r.status_code}): {r.text[:500]}"}
        return {"ok": True, "revoked": connected_account_id}


# ══════════════════════════════════════════════════════════
# 6. أدوات المستندات (مع client_id للعزل)
# ══════════════════════════════════════════════════════════

def create_docx(
    title: str,
    content_markdown: str = "",
    content: str = "",
    text: str = "",
    markdown: str = "",
    filename: str = "",
    request: Optional[Request] = None,
    files_dir: Optional[Path] = None,
    client_id: str = "",
) -> dict:
    from docx import Document
    final_content = _pick_content(content_markdown, content, text, markdown)
    if not final_content:
        raise ValueError("المعامل 'content_markdown' مطلوب لإنشاء DOCX.")
    doc = Document()
    doc.add_heading(title, level=1)
    _apply_markdown_lines(
        lambda t, lvl: doc.add_heading(t, level=lvl),
        lambda t: doc.add_paragraph(t),
        lambda t: doc.add_paragraph(t, style="List Bullet"),
        final_content,
    )
    buf = io.BytesIO()
    doc.save(buf)
    return _save_bytes_and_url(buf.getvalue(), "docx", request, files_dir, filename, client_id)


def create_pptx(
    title: str,
    slides: Optional[List[Dict[str, Any]]] = None,
    content: Optional[List[Dict[str, Any]]] = None,
    content_markdown: str = "",
    filename: str = "",
    request: Optional[Request] = None,
    files_dir: Optional[Path] = None,
    client_id: str = "",
) -> dict:
    from pptx import Presentation
    final_slides = slides or content
    if final_slides is None and content_markdown:
        final_slides = [{"title": title, "bullets": [content_markdown]}]
    if not final_slides:
        raise ValueError("المعامل 'slides' مطلوب لإنشاء PPTX.")
    prs = Presentation()
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = title
    bullet_layout = prs.slide_layouts[1]
    for s in final_slides:
        slide = prs.slides.add_slide(bullet_layout)
        slide.shapes.title.text = s.get("title", "")
        tf = slide.placeholders[1].text_frame
        bullets = s.get("bullets", [])
        for i, b in enumerate(bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = b
    buf = io.BytesIO()
    prs.save(buf)
    return _save_bytes_and_url(buf.getvalue(), "pptx", request, files_dir, filename, client_id)


def create_xlsx(
    title: str,
    headers: Optional[List[str]] = None,
    rows: Optional[List[List[Any]]] = None,
    filename: str = "",
    request: Optional[Request] = None,
    files_dir: Optional[Path] = None,
    client_id: str = "",
) -> dict:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (title or "Sheet1")[:31]
    if headers:
        ws.append(headers)
    for row in (rows or []):
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return _save_bytes_and_url(buf.getvalue(), "xlsx", request, files_dir, filename, client_id)


def markdown_to_html(markdown_text: str = "", text: str = "", content: str = "") -> dict:
    import markdown as md
    final_text = _pick_content(markdown_text, text, content)
    if not final_text:
        raise ValueError("المعامل 'markdown_text' مطلوب.")
    html = md.markdown(final_text, extensions=["tables", "fenced_code"])
    return {"html": html}


# ══════════════════════════════════════════════════════════
# 7. أدوات النصوص (مع client_id للعزل)
# ══════════════════════════════════════════════════════════

def write_text_file(
    content: str = "",
    text: str = "",
    data: str = "",
    ext: str = "txt",
    filename: str = "",
    request: Optional[Request] = None,
    files_dir: Optional[Path] = None,
    client_id: str = "",
) -> dict:
    final_content = _pick_content(content, text, data)
    if not final_content:
        raise ValueError("المعامل 'content' مطلوب.")
    return _save_bytes_and_url(final_content.encode("utf-8"), ext, request, files_dir, filename, client_id)


# ══════════════════════════════════════════════════════════
# 8. Ping
# ══════════════════════════════════════════════════════════

async def ping() -> Dict[str, Any]:
    return {"ok": True, "message": "pong"}


# ══════════════════════════════════════════════════════════
# ★ ما تم حذفه (موجود في main.py مع حماية أفضل):
#   - create_pdf        → في main.py مع _arabic_font_ready + lifespan
#   - read_pdf          → في main.py مع _is_url_safe (حماية SSRF)
#   - fetch_media_to_file → في main.py مع _is_url_safe (حماية SSRF)
#   - pdf_info          → في main.py مع _is_url_safe (حماية SSRF)
#   - read_xlsx         → في main.py مع _is_url_safe (حماية SSRF)
#   - _ensure_arabic_font → في main.py (lifespan)
#   - _shape_text       → في main.py (مشترك مع create_pdf)
#   - _apply_markdown_lines → في main.py (مشترك)
# ══════════════════════════════════════════════════════════