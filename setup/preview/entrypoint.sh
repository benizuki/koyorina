#!/bin/sh
# マウントされた /workspace の生成コードを実行する。依存は /var/preview 側に置き、
# 成果物（ZIP・再生成）へ混ざらないようにする。ネットワークは依存取得にだけ使う。
set -eu

VAR="${PREVIEW_VAR:-/var/preview}"
APP="${PREVIEW_APP:-backend.main:app}"
APP_PORT="${PREVIEW_APP_PORT:-8081}"

export PREVIEW_APP_PORT="$APP_PORT"
export HOME="$VAR/home"
export XDG_CACHE_HOME="$VAR/cache"
export npm_config_cache="$VAR/cache/npm"

# Leave 1 GiB of the 3 GiB container budget for native memory and other processes.
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=2048}"

# db はここだけ別マウント（node-local emptyDir）。RWX(NFS)上のSQLiteは
# fcntlロック絡みで壊れうるため、venv/node_modulesキャッシュとは分けている。
mkdir -p "$HOME" "$VAR/cache" "$VAR/db"

# Pythonとnodeの導入物は別々に判定する。実行環境（CPU種別・処理系）が変わると
# 拡張モジュールは動かないため、その場合は作り直す。導入済みのメタデータを信じて
# 入れ替えない実装があるため、環境が変わったら仮想環境ごと捨てる。
export UV_CACHE_DIR="$VAR/cache/uv" VIRTUAL_ENV="$VAR/venv"

# Python/Nodeの版が同じでもglibc向けの依存はmusl上では使えない。
libc_digest="$(ldd --version 2>&1 | sha256sum | cut -d' ' -f1)"
python_digest="$( { echo preview-python-v4; echo "$libc_digest"; uname -m; python -V; uv --version; cat pyproject.toml backend/requirements.txt 2>/dev/null; } | sha256sum | cut -d' ' -f1)"

node_digest="$( { echo "$libc_digest"; uname -m; node -v; cat frontend/package.json 2>/dev/null; } | sha256sum | cut -d' ' -f1)"

if [ "$(cat "$VAR/python.sha" 2>/dev/null || echo none)" != "$python_digest" ]; then
  echo "[preview] Pythonの依存関係を導入しています。"

  rm -rf "$VIRTUAL_ENV"
  uv venv --quiet "$VIRTUAL_ENV"

  export PATH="$VIRTUAL_ENV/bin:$PATH"

  # 認証・DB・フォームなど、Koyorinaの標準構成に必要な実行依存は必ず入れる。
  # 生成側がrequirements.txtへ1件書き忘れても、プレビュー自体は起動できるようにする。
  uv pip install --quiet fastapi sqlalchemy pydantic "psycopg[binary]" "google-auth[requests]" \
    itsdangerous httpx python-multipart email-validator "uvicorn[standard]"

  # 生成アプリはuvプロジェクト。依存の宣言は pyproject.toml の [project].dependencies
  # ひとつだけにする。-r に pyproject.toml を渡すと、uv はそこから依存だけを読む
  # （アプリ自体をパッケージとしてビルドしない。生成物にビルドバックエンドは無い）。
  if [ -f pyproject.toml ]; then
    uv pip install --quiet -r pyproject.toml
  elif [ -f backend/requirements.txt ]; then
    # pyproject.toml を持たない、以前の規約で作られたアプリ。
    uv pip install --quiet -r backend/requirements.txt
  else
    echo "[preview] pyproject.toml がないため、標準構成の依存だけで起動します。"
  fi

  printf '%s' "$python_digest" > "$VAR/python.sha"

else
  export PATH="$VIRTUAL_ENV/bin:$PATH"
fi

if [ -f frontend/package.json ] && [ "$(cat "$VAR/node.sha" 2>/dev/null || echo none)" != "$node_digest" ]; then
  echo "[preview] 画面の依存関係を導入しています。"

  rm -rf frontend/node_modules
  npm install --prefix frontend --no-audit --no-fund

  printf '%s' "$node_digest" > "$VAR/node.sha"
fi

BASE="${APP_BASE_PATH:-/}"

if [ -x frontend/node_modules/.bin/vite ]; then
  # Koyorina配下のパスで配信するため、資産URLの基点をここで固定する。
  # CLIの --base は生成物の vite.config.ts の設定より優先される。
  echo "[preview] 画面を初回ビルドしています。"
  # npm run buildへ引数を足すと、`vue-tsc --noEmit && vite build`の
  # vue-tsc側にも --base が渡り、TypeScriptがヘルプを表示してビルドが
  # 止まる。型検査とViteビルドを分け、--baseはViteだけへ渡す。
  ( cd frontend && \
    if [ -x node_modules/.bin/vue-tsc ] && [ -f tsconfig.json -o -f tsconfig.app.json ]; then \
      node_modules/.bin/vue-tsc --noEmit; \
    fi && \
    node_modules/.bin/vite build --base "$BASE" )

  echo "[preview] 画面の初回ビルドが完了しました。"
else
  echo "[preview] Viteが見つからないため、画面を起動できません。" >&2
  exit 1
fi

echo "[preview] アプリを起動します: $APP"

# アクセスログは出す。実行状態のログ画面で、要求がアプリまで届いたか分かるようにする。
# 前段は抑止したまま。両方出すと同じ要求が二重に並ぶ。
uvicorn "$APP" --app-dir /workspace --host 127.0.0.1 --port "$APP_PORT" \
  --reload --reload-dir /workspace/backend &

# 追従ビルドはアプリが起動しきってから。生成アプリはたいてい「distがあれば配信、
# 無ければAPIだけ」を**取り込み時に一度だけ**決める。vite の --watch は開始時に
# 出力先を空にするため、先に走らせるとアプリが「distが無い」と判断してしまい、
# 画面は真っ白のままになる（--reload は backend しか見ないので直らない）。
#
# --emptyOutDir false も要る。付けないと、再ビルドのたびに一瞬 dist が消え、
# その隙に来た要求が資産を取り損ねる。
for _ in $(seq 1 60); do
  if python -c "import socket,sys; s=socket.socket(); sys.exit(s.connect_ex(('127.0.0.1', int('$APP_PORT'))))" 2>/dev/null; then
    break
  fi
  sleep 1
done
( cd frontend && exec ./node_modules/.bin/vite build --base "$BASE" --watch \
    --emptyOutDir false --logLevel warn ) &

# 前段が画面を配信し、残りをアプリへ渡す。アプリはループバックのみで待ち受ける。
echo "[preview] 画面を配信します: $FRONTEND_DIST"

exec /opt/preview/venv/bin/uvicorn front:app --app-dir /opt/preview \
  --host 0.0.0.0 --port 8080 --no-access-log
