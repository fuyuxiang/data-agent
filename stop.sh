#!/bin/bash

# stop.sh - 关闭前后端服务

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
FRONTEND_PORT=50803
BACKEND_PORT=50805

kill_pid_file() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local pid
        pid="$(cat "$pid_file" 2>/dev/null)"
        if [ -n "$pid" ] && kill -0 "$pid" > /dev/null 2>&1; then
            kill "$pid" > /dev/null 2>&1 || true
            sleep 1
            if kill -0 "$pid" > /dev/null 2>&1; then
                kill -9 "$pid" > /dev/null 2>&1 || true
            fi
        fi
        rm -f "$pid_file"
    fi
}

kill_by_port() {
    local port="$1"
    local service_name="$2"
    local pids

    pids="$(lsof -ti:"$port" 2>/dev/null)"
    if [ -z "$pids" ]; then
        echo "${service_name}服务未运行"
        return
    fi

    while IFS= read -r pid; do
        [ -n "$pid" ] && kill "$pid" > /dev/null 2>&1 || true
    done <<< "$pids"

    sleep 1
    pids="$(lsof -ti:"$port" 2>/dev/null)"
    if [ -n "$pids" ]; then
        while IFS= read -r pid; do
            [ -n "$pid" ] && kill -9 "$pid" > /dev/null 2>&1 || true
        done <<< "$pids"
    fi

    echo "${service_name}服务已关闭 (端口 ${port})"
}

echo "正在关闭前后端服务..."

# 优先使用 PID 文件关闭
kill_pid_file "$LOG_DIR/frontend.pid"
kill_pid_file "$LOG_DIR/backend.pid"

# 再按端口兜底
echo "检查前端进程 (端口 ${FRONTEND_PORT})..."
kill_by_port "$FRONTEND_PORT" "前端"
echo "检查后端进程 (端口 ${BACKEND_PORT})..."
kill_by_port "$BACKEND_PORT" "后端"

echo "所有服务已关闭"
