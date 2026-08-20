# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Dwell Time Monitor (ONNX Runtime edition).

Build (from the project root):
    pyinstaller dwell_monitor.spec --clean --noconfirm --distpath dist/windows

Output: dist/windows/dwell_monitor/dwell_monitor.exe (onedir bundle)

The bundle contains:
- backend/main.py            -> entry point (FastAPI + uvicorn)
- backend/yolov8n.onnx       -> YOLOv8n detection model (ONNX Runtime)
- frontend/                  -> static web UI (index.html, app.js, ...)
- runtime files (cameras.json, dwell_data.db) are created next to the exe
"""

from pathlib import Path

spec_dir = Path(SPECPATH)
backend_dir = spec_dir / "backend"
frontend_dir = spec_dir / "frontend"

a = Analysis(
    [str(backend_dir / "main.py")],
    pathex=[str(backend_dir)],
    binaries=[],
    datas=[
        # (source, destination_inside_bundle)  -- lands in _internal/
        (str(backend_dir / "yolov8n.onnx"), "."),
        (str(frontend_dir), "frontend"),
    ],
    hiddenimports=[
        # --- web server stack ---
        'uvicorn', 'fastapi', 'pydantic', 'starlette', 'anyio', 'click',
        'h11', 'websockets', 'sqlite3',
        # --- numeric / vision stack ---
        'scipy', 'numpy', 'cv2',
        # --- ONNX inference (PyTorch / ultralytics are NOT needed) ---
        'onnxruntime', 'onnxruntime.capi',
        'onnxruntime.capi.onnxruntime_pybind11_state',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # heavy packages that must never sneak into the bundle
        'torch', 'torchvision', 'ultralytics', 'thop',
        'matplotlib', 'pandas', 'tkinter', 'IPython', 'pytest',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='dwell_monitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,            # keep the console window: closing it stops the app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='dwell_monitor',
)
