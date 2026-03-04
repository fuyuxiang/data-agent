#!/bin/bash

# start.sh - 开启前后端服务

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
LOG_DIR="$PROJECT_DIR/logs"
BACKEND_PORT=50805
FRONTEND_PORT=50803

command_exists() {
    command -v "$1" > /dev/null 2>&1
}

detect_python() {
    if command_exists python3; then
        echo "python3"
    elif command_exists python; then
        echo "python"
    fi
}

# 创建日志目录
mkdir -p "$LOG_DIR"

echo "正在开启前后端服务..."

if [ ! -d "$BACKEND_DIR" ] || [ ! -d "$FRONTEND_DIR" ]; then
    echo "项目目录结构不完整: 需要 backend/ 和 frontend/ 目录"
    exit 1
fi

PYTHON_BIN="$(detect_python)"
if [ -z "$PYTHON_BIN" ]; then
    echo "未找到 Python，请先安装 Python 3"
    exit 1
fi

if ! command_exists npm; then
    echo "未找到 npm，请先安装 Node.js 与 npm"
    exit 1
fi

if ! command_exists node; then
    echo "未找到 node，请先安装 Node.js"
    exit 1
fi

HAS_CURL=0
if command_exists curl; then
    HAS_CURL=1
fi

if ! "$PYTHON_BIN" -m pip --version > /dev/null 2>&1; then
    echo "未找到 pip，请先安装 pip"
    exit 1
fi

# 按需安装后端依赖
if ! "$PYTHON_BIN" -c "import fastapi, uvicorn, pydantic_settings" > /dev/null 2>&1; then
    echo "检测到后端依赖缺失，正在安装..."
    "$PYTHON_BIN" -m pip install -r "$BACKEND_DIR/requirements.txt" || {
        echo "后端依赖安装失败，请手动执行: cd \"$BACKEND_DIR\" && $PYTHON_BIN -m pip install -r requirements.txt"
        exit 1
    }
fi

# 按需安装前端依赖
FRONTEND_NEEDS_INSTALL=0
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    FRONTEND_NEEDS_INSTALL=1
fi
if [ ! -f "$FRONTEND_DIR/node_modules/vite/bin/vite.js" ]; then
    FRONTEND_NEEDS_INSTALL=1
fi

if [ "$FRONTEND_NEEDS_INSTALL" -eq 1 ]; then
    echo "检测到前端依赖缺失，正在安装..."
    (cd "$FRONTEND_DIR" && npm install --no-fund --no-audit) || {
        echo "前端依赖安装失败，请手动执行: cd \"$FRONTEND_DIR\" && npm install"
        exit 1
    }
fi

# 启动后端
echo "启动后端服务 (端口 $BACKEND_PORT)..."
(
    cd "$BACKEND_DIR" || exit 1
    nohup "$PYTHON_BIN" -m uvicorn main:app --host 0.0.0.0 --port "$BACKEND_PORT" > "$LOG_DIR/backend.log" 2>&1 &
    echo "$!" > "$LOG_DIR/backend.pid"
)
BACKEND_PID="$(cat "$LOG_DIR/backend.pid" 2>/dev/null)"
echo "后端服务已启动 (PID: $BACKEND_PID)"

# 等待后端启动
BACKEND_READY=0
for _ in {1..15}; do
    if [ "$HAS_CURL" -eq 1 ] && curl -s "http://localhost:${BACKEND_PORT}/health" > /dev/null 2>&1; then
        BACKEND_READY=1
        break
    fi
    if [ -n "$BACKEND_PID" ] && ! kill -0 "$BACKEND_PID" > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# 检查后端是否启动成功
if [ "$BACKEND_READY" -eq 1 ]; then
    echo "后端服务启动成功"
elif [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" > /dev/null 2>&1; then
    echo "后端进程已启动，但健康检查未通过（可能是启动较慢或当前环境限制本地端口访问）"
else
    echo "后端服务启动失败，请查看 $LOG_DIR/backend.log"
fi

# 启动前端
echo "启动前端服务 (端口 $FRONTEND_PORT)..."
(
    cd "$FRONTEND_DIR" || exit 1
    nohup node "$FRONTEND_DIR/node_modules/vite/bin/vite.js" --host 0.0.0.0 --port "$FRONTEND_PORT" > "$LOG_DIR/frontend.log" 2>&1 &
    echo "$!" > "$LOG_DIR/frontend.pid"
)
FRONTEND_PID="$(cat "$LOG_DIR/frontend.pid" 2>/dev/null)"
echo "前端服务已启动 (PID: $FRONTEND_PID)"

# 等待前端启动
sleep 5

echo ""
echo "========================================"
echo "服务已启动:"
echo "  前端: http://localhost:${FRONTEND_PORT}"
echo "  后端: http://localhost:${BACKEND_PORT}"
echo "  日志: $LOG_DIR"
echo "========================================"
