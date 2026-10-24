# 標準版（Alpine ベース）。既定でこちらを使う。
# 実行用の段を Takumi Images の最小イメージにした版は Dockerfile.takumi。
# 切り替え: make build APP_DOCKERFILE=Dockerfile.takumi
# どちらの版もマニフェストの起動（python -m uvicorn / python -m alembic）で動く。
FROM node:24-alpine AS frontend

WORKDIR /build

# 依存の取得はベースイメージ同梱の古い npm ではなく、版を固定して上げた npm で行う。
# 実行用のイメージには npm を入れないので、ここはビルド時の安全のため。
RUN npm install --global --no-audit --no-fund npm@11.21.1 && npm cache clean --force

COPY frontend/package*.json ./

RUN npm ci

COPY frontend/ ./

ARG VITE_APP_NAME=Koyorina
RUN VITE_APP_NAME="$VITE_APP_NAME" npm run build

# ---
# uv の版は固定する。上げるときはここだけ変える。
FROM ghcr.io/astral-sh/uv:0.13.4 AS uv

# ---
# Python の依存は uv.lock のとおりに venv へ入れる。uv 自体は実行用の段に残さない。
FROM python:3.14-alpine AS backend

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# プロジェクトは virtual（パッケージとしては入れない）。依存だけを入れ、コードは実行用の段で置く。
# --locked: pyproject.toml と uv.lock が食い違っていたらビルドを止める。
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project

# ---
FROM python:3.14-alpine AS runtime

# ベースイメージ公開後に出た OS パッケージの修正を取り込む。
# 実行時は venv の Python を使うので、ベースの pip は消して攻撃面と脆弱性の検出を減らす。
RUN apk upgrade --no-cache \
    && python -m pip uninstall --yes --root-user-action=ignore pip \
    && adduser -D -u 10001 forge

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=backend /opt/venv /opt/venv
COPY alembic.ini ./
COPY backend/ ./backend/
COPY setup/publication/ ./setup/publication/
COPY --from=frontend /build/dist ./frontend/dist/

USER forge

EXPOSE 8080

# 管理APIだけを実行する。Codexや生成コードはこのコンテナに入れない。
CMD ["uvicorn", "backend.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]

# ---
# Docker Compose版（compose.yaml）だけが使う。プレビューを docker で起動するため
# CLIを足す。本番イメージ（下の既定の段）には入れない。
FROM runtime AS compose
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker

# ---
# 既定のビルド対象（本番）。runtime と同じ中身。compose の段より後に置くこと
# （最後の段が既定になるため）。
FROM runtime
