# 標準版（Debian slim ベース）。既定でこちらを使う。
# 実行用の段を Takumi Images の最小イメージにした版は Dockerfile.takumi。
# 切り替え: make build APP_DOCKERFILE=Dockerfile.takumi
# どちらの版もマニフェストの起動（python -m uvicorn / python -m alembic）で動く。
FROM node:24-slim AS frontend

WORKDIR /build

COPY frontend/package*.json ./

RUN npm ci

COPY frontend/ ./

ARG VITE_APP_NAME=Koyorina
RUN VITE_APP_NAME="$VITE_APP_NAME" npm run build

# ---
FROM python:3.14-slim AS runtime

WORKDIR /app

COPY pyproject.toml ./
COPY backend/ ./backend/

RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home forge

COPY alembic.ini ./
COPY --from=frontend /build/dist ./frontend/dist/

USER forge

EXPOSE 8080

# 管理APIだけを実行する。Codexや生成コードはこのコンテナに入れない。
CMD ["uvicorn", "backend.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]

# ---
# Docker Compose版（compose.yaml）だけが使う。プレビューを docker で起動するため
# CLIを足す。本番イメージ（下の既定の段）には入れない。
FROM runtime AS compose
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker

# ---
# 既定のビルド対象（本番）。runtime と同じ中身。compose の段より後に置くこと
# （最後の段が既定になるため）。
FROM runtime
