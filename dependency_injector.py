import os
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
from pathlib import Path

# إعداد نظام التسجيل (Logging) لتتبع إضافة المفاتيح
logger = logging.getLogger(__name__)

@dataclass
class DependencyInjector:
    """يدير التبعيات ويبني وسائط للأدوات"""
    settings: Dict[str, str] = None
    services: Dict[str, Any] = None

    def __post_init__(self):
        if self.settings is None:
            self.settings = {}
        if self.services is None:
            self.services = {}

    def resolve_dependencies(self, tool_name: str, user_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        بناء وسائط الأداة: دمج معاملات المستخدم مع التبعيات المطلوبة.
        - يضيف api_key للأدوات التي تحتاجه إذا لم يوفره المستخدم.
        - يسجل كل خطوة لتسهيل التتبع.
        """
        resolved = dict(user_args)
        logger.info(f"🔧 Resolving dependencies for tool: {tool_name}")

        # التحقق مما إذا كان المستخدم قد قدم مفتاحاً بالفعل
        if "api_key" in resolved:
            logger.info(f"✅ User provided API key for {tool_name}, skipping injection.")
            return resolved

        # --- إضافة المفاتيح بناءً على نوع الأداة ---
        # 1. أدوات Firecrawl (دقيقة)
        if tool_name in ["firecrawl_search", "firecrawl_scrape"]:
            api_key = self.settings.get("FIRECRAWL_API_KEY", "")
            if api_key:
                resolved["api_key"] = api_key
                logger.info(f"✅ Injected FIRECRAWL_API_KEY for {tool_name}")
            else:
                logger.warning(f"⚠️ FIRECRAWL_API_KEY is missing or empty for {tool_name}")

        # 2. أدوات Context7 (مرنة: كل الأدوات التي تبدأ بـ 'context7')
        elif tool_name.startswith("context7"):
            api_key = self.settings.get("CONTEXT7_API_KEY", "")
            if api_key:
                resolved["api_key"] = api_key
                logger.info(f"✅ Injected CONTEXT7_API_KEY for {tool_name}")
            else:
                logger.warning(f"⚠️ CONTEXT7_API_KEY is missing or empty for {tool_name}")

        # 3. أدوات Composio (مرنة: كل الأدوات التي تبدأ بـ 'composio')
        elif tool_name.startswith("composio"):
            api_key = self.settings.get("COMPOSIO_API_KEY", "")
            if api_key:
                resolved["api_key"] = api_key
                logger.info(f"✅ Injected COMPOSIO_API_KEY for {tool_name}")
            else:
                logger.warning(f"⚠️ COMPOSIO_API_KEY is missing or empty for {tool_name}")

        # 4. (اختياري) يمكنك إضافة أدوات أخرى هنا بنفس النمط

        else:
            # الأداة لا تطلب مفتاحاً معروفاً، لا نفعل شيئاً
            logger.debug(f"ℹ️ No API key injection defined for tool: {tool_name}")

        # تحقق نهائي: إذا لم يجد المفتاح حتى الآن، سجل تحذيراً
        if "api_key" not in resolved:
            logger.warning(f"⚠️ No API key resolved for tool: {tool_name}. The call may fail if an API key is required.")

        return resolved


def create_default_injector() -> DependencyInjector:
    """إنشاء Injector مع إعدادات البيئة"""
    # قراءة المفاتيح من متغيرات البيئة مرة واحدة عند بدء التشغيل
    settings = {
        "FIRECRAWL_API_KEY": os.environ.get("FIRECRAWL_API_KEY", ""),
        "CONTEXT7_API_KEY": os.environ.get("CONTEXT7_API_KEY", ""),
        "COMPOSIO_API_KEY": os.environ.get("COMPOSIO_API_KEY", ""),
    }
    logger.info("🏗️ Creating default DependencyInjector with environment settings.")
    return DependencyInjector(settings=settings)