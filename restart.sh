#!/bin/bash

# restart.sh - 重启前后端服务

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_PORT=50803
BACKEND_PORT=50805

echo "========================================"
echo "开始重启服务..."
echo "========================================"

# 先执行 stop
bash "$SCRIPT_DIR/stop.sh"

# 等待一下
sleep 2

# 检查端口是否还在占用
echo ""
echo "检查端口占用情况..."

# 检查前端端口
if lsof -ti:"$FRONTEND_PORT" > /dev/null 2>&1; then
    echo "警告: 端口 ${FRONTEND_PORT} 仍被占用，尝试强制关闭..."
    lsof -ti:"$FRONTEND_PORT" | xargs kill -9 2>/dev/null
fi

# 检查后端端口
if lsof -ti:"$BACKEND_PORT" > /dev/null 2>&1; then
    echo "警告: 端口 ${BACKEND_PORT} 仍被占用，尝试强制关闭..."
    lsof -ti:"$BACKEND_PORT" | xargs kill -9 2>/dev/null
fi

# 等待端口释放
sleep 2

# 执行 start
echo ""
bash "$SCRIPT_DIR/start.sh"
