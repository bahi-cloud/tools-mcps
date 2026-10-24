# dependency_injector.py
# حاقن التبعيات (Dependency Injection) للأدوات المحلية
# ★ يقرأ المفاتيح من متغيرات البيئة وقت الاستدعاء (لا وقت الاستيراد)

import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("dependency_injector")


# ══════════════════════════════════════════════════════════
# خريطة: اسم الأداة → أسماء مفاتيح البيئة المحتملة
# (المفاتيح هنا هي أسماء متغيرات API فقط، وليس URLs)
# ══════════════════════════════════════════════════════════

_TOOL_KEY_MAP = {
    "firecrawl_search":          ("FIRECRAWL_API_KEY",),
    "firecrawl_scrape":          ("FIRECRAWL_API_KEY",),
    "context7_resolve_library":  ("CONTEXT7_API_KEY",),
    "context7_get_docs":         ("CONTEXT7_API_KEY",),
    "composio_list_connected_tools": (
        "COMPOSIO_API_KEY", "COMPOSIO_KEY", "COMPOSIO_TOKEN", "COMPOSIO_API_TOKEN"
    ),
    "composio_list_tools": (
        "COMPOSIO_API_KEY", "COMPOSIO_KEY", "COMPOSIO_TOKEN", "COMPOSIO_API_TOKEN"
    ),
    "composio_execute_tool": (
        "COMPOSIO_API_KEY", "COMPOSIO_KEY", "COMPOSIO_TOKEN", "COMPOSIO_API_TOKEN"
    ),
    "composio_revoke_account": (
        "COMPOSIO_API_KEY", "COMPOSIO_KEY", "COMPOSIO_TOKEN", "COMPOSIO_API_TOKEN"
    ),
}


def _read_env_first(names):
    """يرجع أول قيمة موجودة من قائمة أسماء متغيرات البيئة (وقت الاستدعاء)."""
    for name in names:
        val = os.environ.get(name)
        if val and val.strip():
            return val.strip()
    return None


# ══════════════════════════════════════════════════════════
# DependencyInjector
# ══════════════════════════════════════════════════════════

class DependencyInjector:
    """
    يحقن التبعيات في استدعاءات الأدوات المحلية.
    يقرأ المفاتيح من متغيرات البيئة بناءً على اسم الأداة.
    """

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = settings or {}
        logger.info("🏗️ Creating DependencyInjector with environment settings.")

    def resolve_dependencies(
        self,
        tool_name: str,
        user_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """يدمج user_args مع التبعيات المحقونة (مثل api_key) — وقت الاستدعاء."""
        resolved = dict(user_args or {})

        key_names = _TOOL_KEY_MAP.get(tool_name)
        if key_names:
            if "api_key" not in resolved or not resolved.get("api_key"):
                key_value = _read_env_first(key_names)
                if key_value:
                    resolved["api_key"] = key_value
                    logger.debug(f"🔑 Injected api_key for tool '{tool_name}' from env.")
                else:
                    logger.warning(
                        f"⚠️ No API key found for tool '{tool_name}'. "
                        f"Checked: {', '.join(key_names)}"
                    )

        return resolved


# ══════════════════════════════════════════════════════════
# Factory
# ══════════════════════════════════════════════════════════

def create_default_injector() -> DependencyInjector:
    """ينشئ injector افتراضي يقرأ من متغيرات البيئة."""
    return DependencyInjector()