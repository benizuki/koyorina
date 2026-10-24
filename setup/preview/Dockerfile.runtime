# 生成アプリを実行するための共有ランタイム。アプリのコードは含めず、実行時にマウントする。
# プロジェクトごとに作り直さない。ここを更新したときだけ再ビルドする。
FROM node:24-alpine AS node
# npm はベースイメージ同梱の版だと同梱ライブラリの脆弱性が多い。版を固定して上げる。
RUN npm install --global --no-audit --no-fund npm@12.2.1 && npm cache clean --force


# uv の版は固定する。本体の Dockerfile と揃える。
FROM ghcr.io/astral-sh/uv:0.13.4 AS uv
FROM python:3.14-alpine

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# 依存の導入はuvで行う。プロジェクト本体と同じ道具に揃える。
# ベースイメージ公開後に出た OS パッケージの修正を取り込み、使わない pip は消す。
RUN apk upgrade --no-cache \
    && apk add --no-cache libstdc++ \
    && python -m pip uninstall --yes --root-user-action=ignore pip

COPY --from=uv /uv /uvx /usr/local/bin/

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

 # 前段（画面の配信とアプリへの転送）はイメージ側に持つ。生成コードの実装に依存させない。
RUN uv venv --quiet /opt/preview/venv \
 && uv pip install --quiet --no-cache --compile-bytecode --python /opt/preview/venv/bin/python "uvicorn[standard]" httpx

COPY front.py /opt/preview/front.py
COPY entrypoint.sh /usr/local/bin/preview-entrypoint

RUN chmod 0555 /usr/local/bin/preview-entrypoint /opt/preview/front.py

WORKDIR /workspace

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/preview-entrypoint"]
