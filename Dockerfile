# ---- Stage 1: build Tailwind CSS ----
FROM node:20-alpine AS css-builder
WORKDIR /build
COPY package.json tailwind.config.js ./
RUN npm install --no-audit --no-fund --omit=optional
COPY app/templates ./app/templates
COPY app/help.yaml ./app/help.yaml
COPY app/static/src.css ./app/static/src.css
RUN mkdir -p app/static && \
    npx tailwindcss -i app/static/src.css -o app/static/app.css --minify

# ---- Stage 2: Python runtime ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
COPY --from=css-builder /build/app/static/app.css /srv/app/static/app.css

RUN mkdir -p /data && \
    groupadd -g 1000 app && \
    useradd  -u 1000 -g 1000 -d /srv -s /usr/sbin/nologin app && \
    chown -R app:app /srv /data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
