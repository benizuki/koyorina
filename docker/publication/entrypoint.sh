#!/bin/sh
set -eu
mkdir -p /var/published/db
uvicorn backend.main:app --app-dir /workspace --host 127.0.0.1 --port 8081 &
child=$!
trap 'kill "$child" 2>/dev/null || true' TERM INT EXIT
uvicorn front:app --app-dir /opt/publication --host 0.0.0.0 --port 8080 &
front=$!
trap 'kill "$child" "$front" 2>/dev/null || true' TERM INT EXIT
# Exit if either process fails; Kubernetes then restarts the complete application.
while kill -0 "$child" 2>/dev/null && kill -0 "$front" 2>/dev/null; do sleep 2; done
exit 1
