FROM python:3.12-slim
ARG WITH_OCR=0
ARG WITH_PDF_EXPORT=0

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src TZ=Asia/Shanghai
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN if [ "$WITH_OCR" = 1 ] || [ "$WITH_PDF_EXPORT" = 1 ]; then \
      apt-get update && \
      if [ "$WITH_OCR" = 1 ]; then apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim; fi && \
      if [ "$WITH_PDF_EXPORT" = 1 ]; then apt-get install -y --no-install-recommends libreoffice-writer fonts-noto-cjk; fi && \
      rm -rf /var/lib/apt/lists/*; \
    fi
COPY src ./src
COPY db ./db
COPY prompts ./prompts
RUN groupadd -g 10001 biaoshu && useradd -u 10001 -g biaoshu -m biaoshu && mkdir -p /data/objects && chown -R biaoshu:biaoshu /data
USER biaoshu
EXPOSE 8091
CMD ["python","-m","uvicorn","biaoshu.app:create_app","--factory","--host","0.0.0.0","--port","8091","--no-proxy-headers","--no-access-log"]
