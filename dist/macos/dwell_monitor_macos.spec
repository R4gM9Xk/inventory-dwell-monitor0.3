# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Dwell Time Monitor — macOS (.app bundle, ONNX Runtime).

IMPORTANT: PyInstaller cannot cross-compile. Run this spec ON a Mac:
    bash dist/macos/build_macos.sh        (recommended, does everything)
    # or manually:
    pyinstaller dist/macos/dwell_monitor_macos.spec --clean --noconfirm \
        --distpath dist/macos

Output: dist/macos/Dwell Monitor.app  (+ zipped copy by the build script)

The app is windowed (no terminal window). To see console logs, run the
binary directly in Terminal:
    open "dist/macos/Dwell Monitor.app/Contents/MacOS/dwell_monitor"
"""

from pathlib import Path

# spec lives in <project>/dist/macos/ -> project root is two levels up
spec_dir = Path(SPECPATH)
project_dir = spec_dir.parent.parent
backend_dir = project_dir / "backend"
frontend_dir = project_dir / "frontend"

a = Analysis(
    [str(backend_dir / "main.py")],
    pathex=[str(backend_dir)],
    binaries=[],
    datas=[
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
    console=False,           # windowed .app: double-click, no terminal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,        # build for the current Mac (arm64 or x86_64)
    codesign_identity=None,  # unsigned: first launch needs right-click -> Open
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

app = BUNDLE(
    coll,
    name='Dwell Monitor.app',
    version='0.3.0',
    bundle_identifier='com.dwelltime.monitor',
    info_plist={
        'CFBundleName': 'Dwell Monitor',
        'CFBundleDisplayName': 'Dwell Monitor',
        'CFBundleShortVersionString': '0.3.0',
        'NSHighResolutionCapable': True,
        'NSLocalNetworkUsageDescription':
            'Dwell Monitor scans the local network for RTSP cameras '
            'when you use the LAN scanner feature.',
    },
)
