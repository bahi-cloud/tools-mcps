# استخدم صورة Python رسمية خفيفة
FROM python:3.11-slim

# تعيين متغير البيئة لمنع إنشاء ملفات pyc
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# تعيين دليل العمل
WORKDIR /app

# تثبيت dependencies النظامية (اختياري، قد تحتاجها لبعض مكتبات g4f)
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# نسخ ملف المتطلبات أولاً (للاستفادة من caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي المشروع
COPY . .

# إنشاء مجلد للقطات (snapshots)
RUN mkdir -p /app/snapshots

# فتح المنفذ
EXPOSE 8000

# تشغيل التطبيق
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]