# ── フロントエンドビルド ──────────────────────────────
FROM node:24-alpine AS frontend-builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build
# 成果物: /app/frontend/dist

# ── Python 依存関係ビルド ─────────────────────────────
# ビルドツールは最終イメージに残さないため、venv を作る層を分ける。
FROM python:3.14-alpine AS backend-builder
WORKDIR /app

RUN apk add --no-cache \
    build-base \
    cargo \
    libffi-dev \
    linux-headers \
    openssl-dev

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

COPY backend/requirements.txt .
RUN python -m venv "${VIRTUAL_ENV}" \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── ランタイム ────────────────────────────────────────
FROM python:3.14-alpine
WORKDIR /app

RUN apk add --no-cache \
    libffi \
    libgcc \
    libstdc++ \
    openssl

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

COPY --from=backend-builder /opt/venv /opt/venv

COPY backend/ ./backend/
# Vite のビルド成果物。backend からは ../frontend/dist として参照する。
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

EXPOSE 8080

WORKDIR /app/backend
# --timeout は LLM 呼び出しなど長い処理があるアプリで伸ばす。無ければ 120 で足りる。
CMD ["gunicorn", "main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "2", \
     "--timeout", "300"]
