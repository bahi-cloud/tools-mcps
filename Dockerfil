FROM python:3.11-slim

# معلومات المطور
LABEL maintainer="YourName"
LABEL description="G4F API Server - Free AI Chat"

# إعداد بيئة العمل
WORKDIR /app

# تحديث النظام وتثبيت المتطلبات
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# نسخ المتطلبات أولاً (لتسريع البناء)
COPY requirements.txt .

# تثبيت المكتبات
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# نسخ الكود
COPY . .

# المنفذ
EXPOSE 7860

# فحص الصحة
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# تشغيل التطبيق
CMD ["python", "app.py"]