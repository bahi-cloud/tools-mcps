FROM python:3.11-slim

# مستخدم غير جذري (مطلوب في HF Spaces)
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/user

WORKDIR /home/user/app

# تثبيت الاعتماديات أولاً للاستفادة من طبقات الكاش
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# نسخ كود المشروع
COPY --chown=user . .

# مسار اللقطة في مجلد قابل للكتابة
ENV SNAPSHOT_PATH=/home/user/app/active_models.json

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
