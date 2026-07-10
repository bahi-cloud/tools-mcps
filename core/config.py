"""الإعدادات المركزية للبوابة، تُقرأ من المتغيرات البيئية."""
import os
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """إعدادات التطبيق (Pydantic v2 Settings)."""

    API_KEY: str = Field(default="change-me", description="مفتاح المصادقة الرئيسي")
    DEFAULT_MODEL: str = Field(default="gpt-4o-mini", description="النموذج الافتراضي")
    SCAN_ON_STARTUP: bool = Field(default=True, description="فحص النماذج عند الإقلاع")
    SCAN_INTERVAL_HOURS: float = Field(default=6.0, description="الفاصل بين الفحوصات الدورية")
    REQUEST_TIMEOUT: float = Field(default=120.0, description="مهلة الطلب بالثواني")

    # --- تعديل: فصل ميزانية "استدعاء الأدوات" عن ميزانية "إصلاح التنسيق" ---
    # كانت MAX_TOOL_RETRIES تُستخدم للاثنين معاً، مما كان يوقف مهام متعددة الخطوات مبكراً جداً.
    MAX_TOOL_RETRIES: int = Field(
        default=3, description="[محفوظ للتوافق الخلفي فقط، لم يعد يُستخدم مباشرة في agent_loop]"
    )
    MAX_TOOL_ITERATIONS: int = Field(
        default=8, description="أقصى عدد جولات استدعاء أدوات ناجحة (خطوات الوكيل الفعلية)"
    )
    MAX_FORMAT_RETRIES: int = Field(
        default=3, description="أقصى محاولات إعادة الصياغة عند رد غير صالح/غير مكتمل"
    )

    # --- جديد: تفعيل بروتوكول JSON الصارم بدل heuristics الطول/الكلمات ---
    STRICT_JSON_PROTOCOL: bool = Field(
        default=True,
        description="إجبار النموذج على إرجاع {'final': ...} صراحة عند عدم استدعاء أداة، بدل تخمين النهاية من طول النص",
    )

    # --- جديد: تفعيل حلقة الوكيل في وضع البث (stream=True) ---
    STREAM_USES_AGENT_LOOP: bool = Field(
        default=True,
        description="عند وجود tools في الطلب، استخدم حلقة الوكيل الكاملة حتى في وضع stream بدل التمرير المباشر",
    )

    SNAPSHOT_PATH: str = Field(default="active_models.json", description="مسار لقطة النماذج")

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()