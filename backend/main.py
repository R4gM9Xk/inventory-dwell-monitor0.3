"""
Main Application Module
=======================
FastAPI server serving:
- MJPEG video streams with bounding boxes drawn on frames (multi-camera).
- WebSocket endpoint for real-time JSON tracking data (all cameras merged).
- REST API for historical event queries.
- Static frontend files.

Multi-camera architecture:
- Each camera source runs its own CameraProcessor in a daemon thread.
- Each processor has independent frame queue, tracker, and tracked object state.
- A single WebSocket endpoint merges all cameras' data (with camera_id).
- MJPEG endpoint per camera: /video_feed/{camera_id}
"""

import asyncio
import base64
import io
import json
import os
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from tracker import DetectionProcessor
from database import Database
from scanner import RtspScanner


# ---------------------------------------------------------------------------
# Path resolution helpers (PyInstaller compatibility)
# ---------------------------------------------------------------------------
def get_app_dir():
    """
    Directory for runtime-writable files (cameras.json, dwell_data.db).

    - Frozen (PyInstaller): the directory containing the executable, so user
      data sits next to the .exe/.app and survives re-launches.
    - Development: the backend/ directory (same behaviour as before).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_resource(relative):
    """
    Locate a read-only bundled resource (yolov8n.onnx, frontend/).

    Frozen search order: exe dir -> exe/_internal -> sys._MEIPASS, so the
    resource is found whether it was placed next to the executable or inside
    the PyInstaller bundle. Development: backend/ -> project root.
    """
    rel = Path(relative)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [
            exe_dir / rel,
            exe_dir / "_internal" / rel,
            # macOS .app bundle layout (Contents/Frameworks/_internal)
            exe_dir.parent / "Frameworks" / "_internal" / rel,
            Path(getattr(sys, "_MEIPASS", exe_dir)) / rel,
        ]
        for cand in candidates:
            if cand.exists():
                return cand
        return exe_dir / rel
    here = Path(__file__).resolve().parent
    if (here / rel).exists():
        return here / rel
    return here.parent / rel


# ---------------------------------------------------------------------------
# Camera source configuration
# - Sources are persisted to cameras.json next to this file, so cameras added
#   or removed through the web UI survive server restarts.
# - If no config file exists yet, fall back to environment variables
#   (CAMERA_SOURCES / CAMERA_RTSP_URL), defaulting to the local webcam "0".
# ---------------------------------------------------------------------------
CAMERAS_CONFIG = get_app_dir() / "cameras.json"


def save_camera_sources(sources):
    """Persist the camera source list to the config file."""
    try:
        CAMERAS_CONFIG.write_text(
            json.dumps({"sources": sources}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[WARN] Could not write camera config file: {exc}")


def load_camera_sources():
    """Load camera sources from the config file, env vars, or default webcam."""
    if CAMERAS_CONFIG.exists():
        try:
            data = json.loads(CAMERAS_CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        # An explicit "sources" key wins even when empty (user removed all cameras).
        if isinstance(data, dict) and "sources" in data:
            return [str(s).strip() for s in (data.get("sources") or []) if str(s).strip()]

    _sources_raw = os.environ.get("CAMERA_SOURCES")
    if _sources_raw:
        sources = [s.strip() for s in _sources_raw.split(",") if s.strip()]
    else:
        sources = [os.environ.get("CAMERA_RTSP_URL", "0").strip()]

    save_camera_sources(sources)
    return sources


def parse_source(src):
    """Convert a source string to a webcam index (int) or RTSP/file path."""
    try:
        return int(src)
    except ValueError:
        return src


CAMERA_SOURCES = load_camera_sources()

PROCESS_WIDTH = int(os.environ.get("PROCESS_WIDTH", "640"))
PROCESS_HEIGHT = int(os.environ.get("PROCESS_HEIGHT", "480"))
CONF_THRESHOLD = float(os.environ.get("CONF_THRESHOLD", "0.5"))
YOLO_MODEL = os.environ.get("YOLO_MODEL") or str(find_resource("yolov8n.onnx"))
DEVICE = os.environ.get("DEVICE", "cpu")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# Minimum interval (seconds) between frames pushed to the browser via the
# MJPEG /video_feed streams. 1.0 = the video on the frontend updates once
# per second. Set VIDEO_FRAME_INTERVAL=0 to restore continuous streaming.
VIDEO_FRAME_INTERVAL = float(os.environ.get("VIDEO_FRAME_INTERVAL", "1.0"))

# ---------------------------------------------------------------------------
# Dwell time alert thresholds (in seconds) — runtime-configurable via API
# ---------------------------------------------------------------------------
ALERT_GREEN_MAX = int(os.environ.get("ALERT_GREEN_MAX", "259200"))       # < 3 days
ALERT_YELLOW_MAX = int(os.environ.get("ALERT_YELLOW_MAX", "518400"))    # 3-6 days

ALERT_COLORS = {
    "green":  (0, 200, 0),    # BGR for OpenCV drawing
    "yellow": (0, 200, 200),
    "red":    (0, 0, 200),
}
ALERT_CLASSES = {
    "green":  "#22c55e",
    "yellow": "#eab308",
    "red":    "#ef4444",
}


def get_alert_color(dwell_seconds):
    """Return (bgr_tuple, css_color_class)."""
    if dwell_seconds < ALERT_GREEN_MAX:
        return ALERT_COLORS["green"], ALERT_CLASSES["green"]
    elif dwell_seconds < ALERT_YELLOW_MAX:
        return ALERT_COLORS["yellow"], ALERT_CLASSES["yellow"]
    else:
        return ALERT_COLORS["red"], ALERT_CLASSES["red"]


def get_alert_class(dwell_seconds):
    if dwell_seconds < ALERT_GREEN_MAX:
        return "green"
    elif dwell_seconds < ALERT_YELLOW_MAX:
        return "yellow"
    else:
        return "red"


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(title="Dwell Time Monitor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# CameraProcessor — per-camera video processing unit
# ---------------------------------------------------------------------------
class CameraProcessor:
    """
    Encapsulates video capture, YOLO detection, SORT tracking, frame queue,
    and tracked object state for a single camera.

    Runs its own daemon thread. Multiple processors can operate in parallel.
    """

    def __init__(self, camera_id: int, source, db: Database):
        self.camera_id = camera_id
        self.source = source
        self.db = db

        self.detector: DetectionProcessor | None = None
        self.cap: cv2.VideoCapture | None = None

        self.frame_queue: deque = deque(maxlen=5)
        self.tracked_objects: dict = {}     # track_id -> object dict
        self.lock = threading.Lock()
        self.event_map: dict = {}            # track_id -> DB event id
        self.event_map_lock = threading.Lock()

        self.active = True
        self._thread: threading.Thread | None = None

    # ---- Public API ----

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.active = False
        if self._thread:
            self._thread.join(timeout=3)
        if self.cap:
            self.cap.release()
            self.cap = None
        with self.lock:
            self.tracked_objects.clear()

    def close_events(self):
        """Close all open DB events (called when the camera is removed)."""
        with self.event_map_lock:
            eids = list(self.event_map.values())
            self.event_map.clear()
        for eid in eids:
            self.db.close_event(eid)

    def get_frame(self):
        """Return the latest annotated frame bytes, or None."""
        if self.frame_queue:
            return self.frame_queue[-1]
        return None

    def get_tracked_objects(self):
        """Return a list of tracked objects with camera_id injected."""
        with self.lock:
            objs = list(self.tracked_objects.values())
        result = []
        for o in objs:
            copy = dict(o)
            copy["camera_id"] = self.camera_id
            result.append(copy)
        return result

    # ---- Internal processing loop ----

    def _init_capture(self) -> bool:
        if self.cap:
            self.cap.release()
        self.cap = cv2.VideoCapture(self.source)
        if not self.cap.isOpened():
            print(f"[Camera {self.camera_id}] ERROR: Could not open source: {self.source}")
            return False
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        print(f"[Camera {self.camera_id}] Started. Source: {self.source}")
        return True

    def _run(self):
        self.detector = DetectionProcessor(
            model_path=YOLO_MODEL,
            conf_threshold=CONF_THRESHOLD,
            device=DEVICE,
        )

        if not self._init_capture():
            self.active = False
            return

        while self.active:
            ret, frame = self.cap.read()
            if not ret:
                print(f"[Camera {self.camera_id}] Frame read failed, reconnecting...")
                time.sleep(1)
                self._init_capture()
                continue

            # Resize to processing resolution
            frame = cv2.resize(frame, (PROCESS_WIDTH, PROCESS_HEIGHT))

            # Detection + Tracking
            tracked = self.detector.process_frame(frame)

            # Draw bounding boxes and IDs on frame
            annotated = frame.copy()
            current_ids = set()
            new_tracked = {}

            for obj in tracked:
                obj_id = obj["id"]
                bbox = obj["bbox"]
                dwell = obj["dwell_time"]
                first_seen = obj["first_seen"]
                current_ids.add(obj_id)

                bgr_color, css_color = get_alert_color(dwell)
                alert_class = get_alert_class(dwell)

                # Draw bounding box
                cv2.rectangle(
                    annotated,
                    (bbox[0], bbox[1]),
                    (bbox[2], bbox[3]),
                    bgr_color, 2,
                )

                # Draw ID + dwell time label
                dwell_str = format_dwell(dwell)
                label = f"Cam{self.camera_id}-ID:{obj_id} {dwell_str}"
                label_size, _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
                cv2.rectangle(
                    annotated,
                    (bbox[0], bbox[1] - label_size[1] - 6),
                    (bbox[0] + label_size[0] + 4, bbox[1]),
                    bgr_color, -1,
                )
                cv2.putText(
                    annotated, label,
                    (bbox[0] + 2, bbox[1] - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2,
                )

                # Generate thumbnail
                thumb = self._crop_and_encode_thumbnail(frame, bbox)

                new_tracked[obj_id] = {
                    "id": obj_id,
                    "camera_id": self.camera_id,
                    "bbox": bbox,
                    "dwell_time": dwell,
                    "first_seen": first_seen,
                    "thumbnail": thumb,
                    "alert_color": css_color,
                    "alert_class": alert_class,
                }

                # Log to database
                with self.event_map_lock:
                    if obj_id not in self.event_map:
                        eid = self.db.upsert_track(obj_id, camera_id=self.camera_id)
                        self.event_map[obj_id] = eid
                    else:
                        eid = self.event_map[obj_id]
                        self.db.update_dwell(eid, dwell)

            # Close events for departed objects
            with self.event_map_lock:
                departed = set(self.event_map.keys()) - current_ids
                for oid in departed:
                    self.db.close_event(self.event_map[oid])
                    del self.event_map[oid]

            # Update shared state
            with self.lock:
                self.tracked_objects.clear()
                self.tracked_objects.update(new_tracked)

            # Encode annotated frame as JPEG and push to queue
            _, jpeg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 70])
            self.frame_queue.append(jpeg.tobytes())

        self.cap.release()
        print(f"[Camera {self.camera_id}] Stopped.")

    @staticmethod
    def _crop_and_encode_thumbnail(frame, bbox, target_size=120):
        """Crop bbox region and encode as base64 JPEG."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = bbox
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return None
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        ch, cw = crop.shape[:2]
        scale = target_size / max(ch, cw) if max(ch, cw) > 0 else 1.0
        new_w, new_h = int(cw * scale), int(ch * scale)
        if new_w > 0 and new_h > 0:
            crop = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_AREA)
        _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 75])
        b64 = base64.b64encode(buf).decode("utf-8")
        return f"data:image/jpeg;base64,{b64}"


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
db = Database(db_path=str(get_app_dir() / "dwell_data.db"))
connected_websockets = set()
rtsp_scanner = RtspScanner()

# Create one CameraProcessor per source.
# `cameras` is a dict keyed by a stable, monotonically increasing camera_id so
# that removing a camera never shifts the ids of the remaining ones (video
# feeds, composite keys and DB records stay consistent).
cameras: dict[int, CameraProcessor] = {}
_camera_id_counter = -1


def _next_camera_id() -> int:
    global _camera_id_counter
    _camera_id_counter += 1
    return _camera_id_counter


for _src in CAMERA_SOURCES:
    _cid = _next_camera_id()
    cameras[_cid] = CameraProcessor(camera_id=_cid, source=parse_source(_src), db=db)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
def format_dwell(seconds):
    """Format seconds to D:HH:MM string for video overlay."""
    s = max(0, int(seconds))
    d = s // 86400
    h = (s % 86400) // 3600
    m = (s % 3600) // 60
    return f"{d}:{h:02d}:{m:02d}"


# ---------------------------------------------------------------------------
# MJPEG streaming endpoint (per camera)
# ---------------------------------------------------------------------------
@app.get("/video_feed/{camera_id}")
async def video_feed(camera_id: int):
    """
    Serve MJPEG stream for a specific camera.
    """
    cam = cameras.get(camera_id)
    if cam is None:
        return JSONResponse({"error": "Invalid camera_id"}, status_code=404)

    def generate():
        while cam.active:
            frame = cam.get_frame()
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
                # Throttle the stream so the frontend video updates at most
                # once every VIDEO_FRAME_INTERVAL seconds (default 1 s).
                if VIDEO_FRAME_INTERVAL > 0:
                    time.sleep(VIDEO_FRAME_INTERVAL)
            else:
                time.sleep(0.05)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/video_feed")
async def video_feed_default():
    """Redirect to the first available camera for backward compatibility."""
    first_id = next(iter(cameras), None)
    if first_id is None:
        return JSONResponse({"error": "No cameras available"}, status_code=404)
    return RedirectResponse(f"/video_feed/{first_id}")


# ---------------------------------------------------------------------------
# WebSocket endpoint — merged data from all cameras
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket pushing JSON tracking data from ALL cameras every ~1 second.

    Message format (sent to client):
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
        },
        ...
    ]
    """
    await websocket.accept()
    connected_websockets.add(websocket)
    try:
        while True:
            # Collect data from all cameras
            all_data = []
            for cam in list(cameras.values()):
                all_data.extend(cam.get_tracked_objects())
            await websocket.send_json(all_data)
            await asyncio.sleep(1)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        connected_websockets.discard(websocket)


# ---------------------------------------------------------------------------
# REST API endpoints
# ---------------------------------------------------------------------------
@app.get("/api/cameras")
async def get_cameras():
    """Return list of configured cameras."""
    return [
        {
            "camera_id": cid,
            "source": str(cam.source),
            "active": cam.active,
        }
        for cid, cam in cameras.items()
    ]


@app.post("/api/cameras")
async def add_camera(body: dict):
    """Add a new camera source (RTSP URL, webcam index, or video file)."""
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    source = str(body.get("source", "")).strip()
    if not source:
        return JSONResponse({"error": "source is required"}, status_code=400)

    parsed = parse_source(source)
    if any(cam.active and str(cam.source) == str(parsed) for cam in cameras.values()):
        return JSONResponse({"error": "This camera source is already in use"}, status_code=409)

    cid = _next_camera_id()
    cam = CameraProcessor(camera_id=cid, source=parsed, db=db)
    cameras[cid] = cam
    cam.start()

    save_camera_sources([str(c.source) for c in cameras.values()])
    print(f"[INFO] Added camera {cid}: {source}")
    return JSONResponse(
        {"camera_id": cid, "source": str(cam.source), "active": cam.active},
        status_code=201,
    )


@app.delete("/api/cameras/{camera_id}")
async def remove_camera(camera_id: int):
    """Stop and remove a camera."""
    cam = cameras.get(camera_id)
    if cam is None:
        return JSONResponse({"error": "Invalid camera_id"}, status_code=404)

    cam.close_events()
    cam.stop()
    del cameras[camera_id]

    save_camera_sources([str(c.source) for c in cameras.values()])
    print(f"[INFO] Removed camera {camera_id}: {cam.source}")
    return {"deleted": camera_id}


@app.get("/api/history")
async def get_history(limit: int = 100, offset: int = 0):
    """Return historical (completed) dwell events."""
    rows = db.get_history(limit=limit, offset=offset)
    return [
        {
            "id": r["id"],
            "camera_id": r["camera_id"],
            "track_id": r["track_id"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "dwell_seconds": r["dwell_seconds"],
            "is_active": bool(r["is_active"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get("/api/stats")
async def get_stats():
    """Return aggregate statistics."""
    return db.get_stats()


@app.get("/api/active")
async def get_active():
    """Return currently tracked objects from all cameras."""
    all_data = []
    for cam in cameras.values():
        all_data.extend(cam.get_tracked_objects())
    return all_data


@app.get("/api/config")
async def get_config():
    """Return current alert threshold configuration."""
    return {
        "alert_green_max": ALERT_GREEN_MAX,
        "alert_yellow_max": ALERT_YELLOW_MAX,
    }


@app.post("/api/config")
async def set_config(body: dict):
    """Update alert thresholds at runtime (applies to all cameras)."""
    global ALERT_GREEN_MAX, ALERT_YELLOW_MAX
    if "alert_green_max" in body:
        val = int(body["alert_green_max"])
        if val > 0:
            ALERT_GREEN_MAX = val
    if "alert_yellow_max" in body:
        val = int(body["alert_yellow_max"])
        if val > 0:
            ALERT_YELLOW_MAX = val
    return {
        "alert_green_max": ALERT_GREEN_MAX,
        "alert_yellow_max": ALERT_YELLOW_MAX,
    }


# ---------------------------------------------------------------------------
# RTSP network scanner endpoints
# ---------------------------------------------------------------------------
@app.post("/api/scan/start")
async def scan_start(body: dict):
    """Start a LAN RTSP camera scan (background thread)."""
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    # Blank target -> the scanner auto-detects the local LAN(s).
    target = str(body.get("target") or body.get("cidr") or "").strip()

    ports = body.get("ports")  # optional: [554, 8554] or "554,8554"
    username = str(body.get("username") or "").strip()
    # password is intentionally not stripped: an empty password is a valid
    # credential (e.g. "admin" with empty password).
    password = "" if body.get("password") is None else str(body.get("password"))
    # If a username is supplied, scan only with that credential pair; otherwise
    # fall back to the built-in list of common default credentials.
    credentials = [[username, password]] if username else None
    timeout = body.get("timeout", 1.0)
    try:
        timeout = max(0.3, min(10.0, float(timeout)))
    except (TypeError, ValueError):
        timeout = 1.0

    ok, message = rtsp_scanner.start(
        target, ports=ports, credentials=credentials, timeout=timeout)
    if not ok:
        code = 409 if "already running" in message else 400
        return JSONResponse({"error": message}, status_code=code)
    return {"started": True, "message": message}


@app.get("/api/scan/status")
async def scan_status():
    """Return current scan progress, log lines and discovered cameras."""
    return rtsp_scanner.status()


@app.post("/api/scan/stop")
async def scan_stop():
    """Request the running scan to stop."""
    rtsp_scanner.stop()
    return {"stopping": True}


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------
def _open_browser_delayed():
    """
    Open the default browser at the web UI shortly after startup.

    - Uses 127.0.0.1 because HOST may be 0.0.0.0 (not directly browsable).
    - Delayed via threading.Timer so the server has time to accept requests.
    - Set DWELL_NO_BROWSER=1 to disable (e.g. for headless machines).
    """
    if os.environ.get("DWELL_NO_BROWSER", "").strip().lower() in ("1", "true", "yes"):
        print("[INFO] Browser auto-open disabled (DWELL_NO_BROWSER is set)")
        return
    url = f"http://127.0.0.1:{PORT}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"[INFO] Opening browser at {url} (set DWELL_NO_BROWSER=1 to disable)")


@app.on_event("startup")
async def startup():
    """Start all camera processors."""
    for cam in cameras.values():
        cam.start()
    print(f"[INFO] Server starting on {HOST}:{PORT} with {len(cameras)} camera(s)")
    _open_browser_delayed()


@app.on_event("shutdown")
async def shutdown():
    """Clean up resources."""
    for cam in cameras.values():
        cam.stop()
    db.close()


# ---------------------------------------------------------------------------
# Static file serving for frontend
# ---------------------------------------------------------------------------
frontend_dir = Path(find_resource("frontend"))
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
