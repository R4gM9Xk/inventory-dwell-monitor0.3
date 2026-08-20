#!/usr/bin/env bash
# =============================================================================
# Dwell Time Monitor — macOS 一键构建脚本
#
# 用法: 把整个项目文件夹拷到任意一台 Mac (macOS 11+, Intel 或 Apple Silicon),
#       然后在项目根目录执行:
#
#           bash dist/macos/build_macos.sh
#
# 产出: dist/macos/Dwell Monitor.app
#       dist/macos/dwell_monitor_macos.zip   (可直接分发)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT"

# --- 1. find a Python >= 3.10 -----------------------------------------------
PY=""
for cand in python3 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            PY="$cand"; break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "ERROR: 未找到 Python >= 3.10 (brew install python@3.12 后重试)"
    exit 1
fi
echo "[1/6] 使用 $PY ($($PY --version 2>&1))"

# --- 2. clean venv ------------------------------------------------------------
if [ ! -d ".venv-macos-build" ]; then
    "$PY" -m venv .venv-macos-build
fi
# shellcheck disable=SC1091
source .venv-macos-build/bin/activate
python -m pip install --upgrade pip -q
echo "[2/6] 安装依赖 (onnxruntime 等, 首次约 2~4 分钟)..."
python -m pip install -q fastapi "uvicorn[standard]" opencv-python numpy scipy \
    onnxruntime Pillow pyinstaller

# --- 3. sanity checks ---------------------------------------------------------
[ -f backend/main.py ]          || { echo "ERROR: 找不到 backend/main.py"; exit 1; }
[ -f backend/yolov8n.onnx ]     || { echo "ERROR: 找不到 backend/yolov8n.onnx (模型文件)"; exit 1; }
[ -f frontend/index.html ]      || { echo "ERROR: 找不到 frontend/index.html"; exit 1; }
echo "[3/6] 源码与模型检查通过"

# --- 4. pyinstaller -----------------------------------------------------------
echo "[4/6] PyInstaller 打包中..."
pyinstaller dist/macos/dwell_monitor_macos.spec \
    --clean --noconfirm --distpath dist/macos --workpath work/build_macos

# --- 5. tidy up ---------------------------------------------------------------
# spec 同时产出裸目录 dwell_monitor/ 和 Dwell Monitor.app, 只保留 .app
rm -rf "dist/macos/dwell_monitor"
APP="dist/macos/Dwell Monitor.app"
[ -d "$APP" ] || { echo "ERROR: 未生成 $APP"; exit 1; }
# 把模型放到二进制旁边, 方便用户直接替换 (find_resource 优先读取这一份)
cp backend/yolov8n.onnx "$APP/Contents/MacOS/yolov8n.onnx"
echo "[5/6] 生成 $APP"

# --- 6. zip for distribution --------------------------------------------------
rm -f dist/macos/dwell_monitor_macos.zip
cd dist/macos
zip -qry dwell_monitor_macos.zip "Dwell Monitor.app"
cd "$PROJECT"
echo "[6/6] 完成: dist/macos/dwell_monitor_macos.zip"

cat <<'EOF'

====================================================================
 构建完成!
   App:  dist/macos/Dwell Monitor.app
   分发: dist/macos/dwell_monitor_macos.zip

 使用方式 (最终用户):
   1. 把 "Dwell Monitor.app" 拷到"应用程序"或任意文件夹
   2. 首次运行【右键点击】App -> "打开" -> 再点"打开"
      (未签名应用, 直接双击会被 Gatekeeper 拦截, 仅首次需要)
   3. 浏览器会自动打开 http://127.0.0.1:8000
   4. 数据文件 (cameras.json / dwell_data.db) 位于
      App 包内: 右键 App -> 显示包内容 -> Contents/MacOS/
   5. 查看运行日志: 终端执行
      "/Applications/Dwell Monitor.app/Contents/MacOS/dwell_monitor"
====================================================================
EOF
