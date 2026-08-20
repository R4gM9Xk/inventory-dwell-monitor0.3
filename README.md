# Dwell Time Monitor

Real-time inventory dwell time monitoring using **one or more IP cameras** with computer vision.

## Architecture

```
backend/              # Python FastAPI backend
  main.py             # FastAPI server, CameraProcessor, WebSocket, MJPEG, REST API
  tracker.py          # YOLOv8 detection + SORT Kalman filter tracking
  database.py         # SQLite event logging (multi-camera)
  cameras.json        # Persisted camera list (auto-created, managed via the UI)
  requirements.txt    # Python dependencies

frontend/             # Static frontend
  index.html          # Single-page application (multi-camera tabs)
  styles.css          # Responsive styling (dark theme)
  app.js              # WebSocket client, camera switching, real-time UI
```

## How It Works

1. **Video Input**: Connects to one or more IP cameras via RTSP (or webcam) using OpenCV.
2. **Object Detection**: YOLOv8 detects objects in each frame, per camera.
3. **Multi-Object Tracking**: SORT assigns unique IDs per-camera. IDs may overlap between cameras — the composite key `(camera_id, object_id)` ensures uniqueness.
4. **Dwell Time**: Each tracked object records its `first_seen` timestamp. Dwell time = current time - first_seen.
5. **Real-time Updates**: WebSocket pushes merged tracking data from all cameras every ~1 second. Frontend updates dwell time counters every minute in `D:HH:MM` format.
6. **Camera Switching**: Frontend tabs allow switching between cameras for video feed and object list.
7. **Alert Thresholds**:
   - Green: < 3 days
   - Yellow: 3-6 days
   - Red: > 6 days

## Setup

### Prerequisites

- Python 3.10+
- One or more webcams / IP cameras with RTSP stream

### Installation

```bash
# 1. Create a virtual environment
python -m venv venv

# 2. Activate it
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# 3. Install dependencies
pip install -r backend/requirements.txt
```

### Configuration

Set environment variables (or use defaults):

| Variable | Default | Description |
|---|---|---|
| `CAMERA_SOURCES` | `0` (webcam) | Comma-separated list: `0,rtsp://user:pass@192.168.1.100/stream` |
| `CAMERA_RTSP_URL` | `0` (webcam) | Single camera (backward compat, overridden by `CAMERA_SOURCES`) |
| `PROCESS_WIDTH` | `640` | Processing resolution width |
| `PROCESS_HEIGHT` | `480` | Processing resolution height |
| `CONF_THRESHOLD` | `0.5` | YOLO detection confidence threshold |
| `ALERT_GREEN_MAX` | `259200` | Green alert threshold in seconds (< this → green, default 3 days) |
| `ALERT_YELLOW_MAX` | `518400` | Yellow alert threshold in seconds (between green and this → yellow, above → red, default 6 days) |
| `YOLO_MODEL` | `yolov8n.onnx` | Path to the YOLOv8 ONNX model (export from `.pt` via `yolo export format=onnx`, e.g. `yolov8s.onnx` for better accuracy) |
| `DEVICE` | `cpu` | Inference device (`cpu` or `cuda`) |
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |

> **Note:** On first start the effective camera sources are persisted to `backend/cameras.json`. Afterwards that file is the source of truth — cameras added or removed via the web UI survive restarts, and `CAMERA_SOURCES` / `CAMERA_RTSP_URL` are only consulted when no config file exists.

### Managing Cameras from the Web UI

Click the **⚙ Settings** button in the top-right corner of the web UI to open the settings dialog:

- **Camera Management** — lists every configured camera (name, source, running status). Add a camera by entering an RTSP URL (or a local webcam index like `0`) and clicking **Add Camera**; remove one with its **Remove** button (confirmed). Changes take effect immediately — new camera tabs appear and feeds start/stop without restarting the server.
- **Network Camera Scanner** — discovers RTSP cameras on your LAN. All fields are optional: leave everything blank and just click **Scan** — the backend auto-detects the local LAN(s) (e.g. `192.168.0.0/24`) and probes **100 common RTSP ports** (`554`, the `N554` vendor alternates, `5540-5543`, the `855x` series, `7070`, ...) with common default credentials (`admin/12345`, `admin`/empty, ...) and stream paths (`/live.sdp`, `/stream1`, `/h264`, ...). Entering **only a username and password** also works — the scanner sweeps the whole LAN with that exact credential pair. You can narrow it down with a target (CIDR `192.168.1.0/24`, range `192.168.1.10-50`, single IP, or comma-separated combination) or a custom port list. The backend multi-threadedly port-scans every host; progress is shown live, and every working `rtsp://user:pass@ip:port/path` URL is printed to the server console and listed with **Copy** / **Add** buttons for one-click camera registration. Click **Stop** to abort a running scan.
- **Alert Thresholds** — edit the green/yellow dwell thresholds **in hours** (inputs are labeled `(hours)`, defaults 72 h / 144 h) and click **Apply**; cards and video boxes recolor immediately.

### Running — Single Camera

```bash
# Use webcam (default)
python backend/main.py

# Or use RTSP camera
set CAMERA_RTSP_URL=rtsp://username:password@192.168.1.100:554/stream
python backend/main.py
```

### Running — Multiple Cameras

```bash
# Windows — comma separated list
set CAMERA_SOURCES=0,rtsp://admin:pass@192.168.1.101:554/stream,rtsp://admin:pass@192.168.1.102:554/stream
python backend/main.py

# macOS/Linux
export CAMERA_SOURCES="0,rtsp://admin:pass@192.168.1.101:554/stream,rtsp://admin:pass@192.168.1.102:554/stream"
python backend/main.py
```

The frontend will show camera tabs to switch between live feeds and object lists.

Open your browser to **http://localhost:8000**

### Using with a Video File (Demo)

Mix video files with camera sources:
```bash
set CAMERA_SOURCES=path/to/sample1.mp4,path/to/sample2.mp4
python backend/main.py
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Frontend (served as static files) |
| `GET /video_feed` | Redirects to the first available camera's MJPEG stream |
| `GET /video_feed/{camera_id}` | MJPEG stream for a specific camera |
| `WS /ws` | WebSocket: merged real-time tracking JSON (all cameras) |
| `GET /api/history?limit=100&offset=0` | Historical event records |
| `GET /api/stats` | Aggregate statistics |
| `GET /api/active` | Current tracked objects from all cameras |
| `GET /api/cameras` | List configured cameras |
| `POST /api/cameras` | Add a camera, body `{"source": "rtsp://..."}` or `{"source": "0"}` |
| `DELETE /api/cameras/{camera_id}` | Stop and remove a camera |
| `POST /api/scan/start` | Start LAN RTSP scan, body `{"target": "192.168.1.0/24", "ports": [554, 8554], "username": "admin", "password": "12345", "timeout": 1.0}` — all fields optional: blank `target` auto-detects local LAN(s), omitted `ports` sweeps 100 common RTSP ports, omitted `username` tries common default credentials |
| `GET /api/scan/status` | Scan progress, log lines and discovered camera URLs |
| `POST /api/scan/stop` | Abort the running scan |

## WebSocket Message Format

```json
[
  {
    "id": 1,
    "camera_id": 0,
    "bbox": [10, 20, 100, 150],
    "dwell_time": 123.4,
    "first_seen": 1700000000.0,
    "thumbnail": "data:image/jpeg;base64,...",
    "alert_color": "#ef4444",
    "alert_class": "red"
  }
]
```

Each object now carries a `camera_id` field. The frontend uses `camera_id + "-" + id` as the composite key.

## Notes

- For IP cameras, ensure the RTSP stream is accessible from the machine running this application.
- Each camera runs its own independent YOLO detection + SORT tracking thread.
- **Inference uses ONNX Runtime** (no PyTorch/ultralytics needed at runtime). The bundled model is `backend/yolov8n.onnx` (static 1x3x640x640 input, output `(1, 84, 8400)`).
- To swap models, export another variant (`yolo export model=yolov8s.pt format=onnx imgsz=640 opset=13`) and point `YOLO_MODEL` at it — or replace the `yolov8n.onnx` file sitting next to the executable.
- GPU acceleration: install `onnxruntime-gpu` and set `DEVICE=cuda` (the CPU build automatically falls back to CPU when CUDA is unavailable).
- SORT tracking parameters (`max_age`, `min_hits`) are configured in `tracker.py`.

## Packaging (PyInstaller)

Cross-platform bundles are built from the two spec files (inference is ONNX Runtime only, so the bundle is ~300 MB instead of >1 GB):

```bash
# Windows (.exe) — from the project root on Windows
pyinstaller dwell_monitor.spec --clean --noconfirm --distpath dist/windows

# macOS (.app) — from the project root on a Mac
bash dist/macos/build_macos.sh
```

The model file and `frontend/` are bundled automatically; `cameras.json` and `dwell_data.db` are created next to the executable at first run.
