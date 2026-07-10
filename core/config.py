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
    MAX_TOOL_RETRIES: int = Field(default=3, description="أقصى محاولات حلقة الوكيل")
    SNAPSHOT_PATH: str = Field(default="active_models.json", description="مسار لقطة النماذج")

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
