FROM node:20-slim

# تثبيت n8n بشكل عالمي
RUN npm install -g n8n

# إعداد متغيرات البيئة الأساسية لـ Hugging Face
ENV N8N_PORT=7860
ENV N8N_HOST=0.0.0.0
ENV N8N_PROTOCOL=https
ENV N8N_LISTEN_ADDRESS=0.0.0.0

# فتح البورت المطلوب
EXPOSE 7860

# تشغيل n8n مباشرة
CMD ["n8n", "start"]