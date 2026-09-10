#!/usr/bin/env bash
# Orchestrates the four services + their Redis event bus.
#
#   ./run.sh            install-if-missing, start collector+aggregator+bettor
#                        in the background, then attach the display service
#                        to this terminal (Ctrl+C stops *watching* — the
#                        background services keep running so the display can
#                        be reattached, or another one started, without
#                        losing any in-flight match state).
#   ./run.sh stop        stop collector + aggregator + bettor.
#   ./run.sh status       what's running.
#   ./run.sh restart      stop then start.
set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv
PIDDIR=run
mkdir -p "$PIDDIR" logs data

ensure_venv() {
    if [ ! -d "$VENV" ]; then
        echo "[setup] creating virtualenv..."
        python3 -m venv "$VENV"
    fi
    echo "[setup] installing dependencies..."
    "$VENV/bin/pip" install -q -r requirements.txt
}

ensure_redis() {
    if redis-cli ping >/dev/null 2>&1; then
        echo "[setup] redis already running"
        return
    fi
    echo "[setup] starting local redis-server..."
    redis-server --daemonize yes --port 6379 --logfile "$PWD/logs/redis.log"
    for _ in $(seq 1 20); do
        redis-cli ping >/dev/null 2>&1 && return
        sleep 0.2
    done
    echo "[setup] redis-server did not come up in time" >&2
    exit 1
}

start_bg() {
    local name="$1" module="$2"
    if [ -f "$PIDDIR/$name.pid" ] && kill -0 "$(cat "$PIDDIR/$name.pid")" 2>/dev/null; then
        echo "[start] $name already running (pid $(cat "$PIDDIR/$name.pid"))"
        return
    fi
    echo "[start] $name"
    nohup "$VENV/bin/python" -m "$module" >>"logs/$name.out" 2>&1 &
    echo $! >"$PIDDIR/$name.pid"
}

stop_bg() {
    local name="$1"
    if [ -f "$PIDDIR/$name.pid" ]; then
        local pid
        pid="$(cat "$PIDDIR/$name.pid")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "[stop] $name (pid $pid)"
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$PIDDIR/$name.pid"
    fi
}

status() {
    for name in collector aggregator bettor; do
        if [ -f "$PIDDIR/$name.pid" ] && kill -0 "$(cat "$PIDDIR/$name.pid")" 2>/dev/null; then
            echo "$name: running (pid $(cat "$PIDDIR/$name.pid"))"
        else
            echo "$name: stopped"
        fi
    done
    redis-cli ping >/dev/null 2>&1 && echo "redis: running" || echo "redis: not running"
}

cmd="${1:-start}"
case "$cmd" in
start)
    ensure_venv
    ensure_redis
    start_bg collector services.collector.main
    start_bg aggregator services.aggregator.main
    start_bg bettor services.bettor.main
    echo
    echo "[display] attaching — Ctrl+C stops watching only; run './run.sh stop' to fully stop the bot"
    echo
    exec "$VENV/bin/python" -m services.display.main
    ;;
stop)
    stop_bg collector
    stop_bg aggregator
    stop_bg bettor
    echo "[stop] redis-server left running (shared resource) — 'redis-cli shutdown' to stop it too"
    ;;
status)
    status
    ;;
restart)
    "$0" stop
    "$0" start
    ;;
*)
    echo "usage: $0 {start|stop|status|restart}" >&2
    exit 1
    ;;
esac
