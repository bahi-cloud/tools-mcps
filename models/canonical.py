"""الصيغة الموحدة الداخلية لاستدعاءات الأدوات."""
import uuid
from typing import Any, Optional
from pydantic import BaseModel, Field


class CanonicalToolCall(BaseModel):
    """استدعاء أداة مُطبَّع: الوسائط دائماً dict صالح، مع حالة تحقق."""

    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:16]}")
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    valid: bool = True
    rejection_reason: Optional[str] = None

    def to_openai(self) -> dict:
        """تحويل إلى صيغة OpenAI tool_call."""
        import json
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)},
        }
