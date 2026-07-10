---
title: G4F OpenAI Gateway
emoji: 🚀
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# G4F OpenAI-Compatible Gateway

بوابة (Gateway) متوافقة مع OpenAI API مبنية على FastAPI و g4f، مع نظام وكيل ذكي
يدعم الأدوات (Tools)، الفحص الصحي للنماذج، وكشف الوعود الفارغة.

## المميزات

- ✅ نقطة نهاية `/v1/chat/completions` متوافقة مع OpenAI
- ✅ دعم التدفق (SSE Streaming)
- ✅ اكتشاف ديناميكي للنماذج مع فحص صحي دوري
- ✅ تطبيع استدعاءات الأدوات وإصلاح JSON المكسور تلقائياً
- ✅ حلقة تنفيذ ذكية مع كشف الوعود الفارغة (Hedging Detection)
- ✅ مصادقة عبر API Key

## المتغيرات البيئية

| المتغير | الوصف | الافتراضي |
|---------|-------|-----------|
| `API_KEY` | مفتاح المصادقة (أضفه كـ Secret) | `change-me` |
| `DEFAULT_MODEL` | النموذج الافتراضي | `gpt-4o-mini` |
| `SCAN_ON_STARTUP` | فحص النماذج عند الإقلاع | `true` |
| `SCAN_INTERVAL_HOURS` | الفاصل بين الفحوصات الدورية | `6` |
| `REQUEST_TIMEOUT` | مهلة الطلب بالثواني | `120` |
| `MAX_TOOL_RETRIES` | أقصى محاولات حلقة الوكيل | `3` |

> 💡 يُنصح بضبط `SCAN_ON_STARTUP=false` على الخطة المجانية لتسريع الإقلاع.

## الاستخدام

```bash
curl https://<username>-<space-name>.hf.space/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"مرحبا"}]}'
