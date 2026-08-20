"""
Object Detection and Multi-Object Tracking Module
=================================================
Uses a YOLOv8 ONNX model via ONNX Runtime for object detection and a
SORT-like Kalman filter tracker for multi-object tracking across frames.

The ONNX model (yolov8n.onnx, exported from yolov8n.pt) has:
- Input : "images" float32 tensor of shape (1, 3, 640, 640), RGB, [0, 1].
- Output: "output0" float32 tensor of shape (1, 84, 8400) --
          4 box values (cx, cy, w, h in 640-pixel letterbox space) plus
          80 sigmoid-activated class scores for each of 8400 anchors.

Dwell Time Logic:
- Each tracked object receives a unique ID upon first detection.
- `first_seen` timestamp is recorded when the object enters the frame.
- `dwell_time = current_time - first_seen` is calculated each frame.
- If an object leaves the frame (track lost), the track is removed after
  `max_age` frames of no detection match.
- If the same object reappears later, it gets a NEW ID and the timer restarts.
- Short occlusions are handled by Kalman filter prediction for up to
  `max_age` frames before track termination.
"""

import time
import cv2
import numpy as np
import onnxruntime as ort
from scipy.optimize import linear_sum_assignment


def iou_batch(bb_test, bb_gt):
    """
    Compute Intersection-over-Union between two sets of bounding boxes.

    Args:
        bb_test: (N, 4) array of [x1, y1, x2, y2]
        bb_gt: (M, 4) array of [x1, y1, x2, y2]

    Returns:
        (N, M) IoU matrix
    """
    bb_gt = np.expand_dims(bb_gt, 0)
    bb_test = np.expand_dims(bb_test, 1)

    xx1 = np.maximum(bb_test[..., 0], bb_gt[..., 0])
    yy1 = np.maximum(bb_test[..., 1], bb_gt[..., 1])
    xx2 = np.minimum(bb_test[..., 2], bb_gt[..., 2])
    yy2 = np.minimum(bb_test[..., 3], bb_gt[..., 3])

    w = np.maximum(0.0, xx2 - xx1)
    h = np.maximum(0.0, yy2 - yy1)
    wh = w * h

    area_test = (bb_test[..., 2] - bb_test[..., 0]) * (bb_test[..., 3] - bb_test[..., 1])
    area_gt = (bb_gt[..., 2] - bb_gt[..., 0]) * (bb_gt[..., 3] - bb_gt[..., 1])
    union = area_test + area_gt - wh

    return wh / np.maximum(union, 1e-12)


def convert_bbox_to_z(bbox):
    """
    Convert [x1, y1, x2, y2] to Kalman state [cx, cy, s, r].
    cx, cy: bounding box center coordinates
    s: scale (area in pixels)
    r: aspect ratio (width / height)
    """
    w = float(bbox[2] - bbox[0])
    h = float(bbox[3] - bbox[1])
    x = float(bbox[0] + bbox[2]) / 2.0
    y = float(bbox[1] + bbox[3]) / 2.0
    s = w * h
    r = w / h if h > 0 else 1.0
    return np.array([x, y, s, r]).reshape((4, 1))


def convert_x_to_bbox(x, score=None):
    """
    Convert Kalman state [cx, cy, s, r] back to [x1, y1, x2, y2].
    """
    w = np.sqrt(x[2] * x[3]) if x[3] > 0 else np.sqrt(x[2])
    h = x[2] / w if w > 0 else 0
    x1 = x[0] - w / 2.0
    y1 = x[1] - h / 2.0
    x2 = x[0] + w / 2.0
    y2 = x[1] + h / 2.0

    if score is None:
        return np.array([[x1, y1, x2, y2]]).reshape((1, 4))
    else:
        return np.array([[x1, y1, x2, y2, score]]).reshape((1, 5))


class KalmanBoxTracker:
    """
    Kalman filter for an individual tracked object.
    Uses a constant-velocity model over [cx, cy, s, r] with velocity terms
    [dx, dy, ds, dr] for smooth motion prediction during occlusions.

    The Kalman filter provides:
    - State prediction when the object is temporarily undetected.
    - State correction when the detection reappears.
    - Smoothing of noisy bounding box detections.
    """

    count = 0  # Class-level counter for unique track IDs

    def __init__(self, bbox):
        """
        Initialize a new tracker with an initial detection bounding box.

        Args:
            bbox: [x1, y1, x2, y2] -- the first detection for this object.
        """
        # State vector: [cx, cy, s, r, dx, dy, ds, dr]^T
        self.x = np.zeros((8, 1))
        z = convert_bbox_to_z(bbox)
        self.x[:4] = z

        # State transition matrix (constant velocity model)
        self.F = np.array([
            [1, 0, 0, 0, 1, 0, 0, 0],
            [0, 1, 0, 0, 0, 1, 0, 0],
            [0, 0, 1, 0, 0, 0, 1, 0],
            [0, 0, 0, 1, 0, 0, 0, 1],
            [0, 0, 0, 0, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 1, 0],
            [0, 0, 0, 0, 0, 0, 0, 1],
        ], dtype=np.float64)

        # Measurement matrix: we observe [cx, cy, s, r]
        self.H = np.array([
            [1, 0, 0, 0, 0, 0, 0, 0],
            [0, 1, 0, 0, 0, 0, 0, 0],
            [0, 0, 1, 0, 0, 0, 0, 0],
            [0, 0, 0, 1, 0, 0, 0, 0],
        ], dtype=np.float64)

        # Measurement noise covariance (higher noise for area and ratio)
        self.R = np.eye(4, dtype=np.float64) * 1.0
        self.R[2:, 2:] *= 10.0

        # Initial state covariance (high uncertainty for velocity terms)
        self.P = np.eye(8, dtype=np.float64) * 10.0
        self.P[4:, 4:] *= 1000.0

        # Process noise covariance
        self.Q = np.eye(8, dtype=np.float64) * 0.01
        self.Q[4:, 4:] *= 0.01

        # Identity matrix for covariance update
        self.I = np.eye(8, dtype=np.float64)

        # --- Track bookkeeping ---
        self.time_since_update = 0  # Frames since last detection match
        self.id = KalmanBoxTracker.count
        KalmanBoxTracker.count += 1
        self.history = []       # Stores predicted states for this track
        self.hits = 0           # Total number of detection matches
        self.hit_streak = 0     # Consecutive matches (for track confirmation)
        self.age = 0            # Total frames this track has existed

        # --- Dwell time tracking ---
        # first_seen: epoch time when the object first appeared
        # last_seen: epoch time of the most recent detection
        self.first_seen = time.time()
        self.last_seen = time.time()

    def update(self, bbox):
        """
        Update the Kalman filter with a new detection (correction step).

        Args:
            bbox: [x1, y1, x2, y2] from the detector.
        """
        self.time_since_update = 0
        self.history = []
        self.hits += 1
        self.hit_streak += 1
        self.last_seen = time.time()

        z = convert_bbox_to_z(bbox)

        # Innovation (residual between prediction and measurement)
        y = z - self.H @ self.x

        # Innovation covariance
        S = self.H @ self.P @ self.H.T + self.R

        # Kalman gain
        K = self.P @ self.H.T @ np.linalg.inv(S)

        # State update
        self.x = self.x + K @ y

        # Covariance update (Joseph form)
        self.P = (self.I - K @ self.H) @ self.P

    def predict(self):
        """
        Advance the Kalman filter state (prediction step).
        Called each frame regardless of whether a detection match exists.

        Returns:
            Predicted bounding box [x1, y1, x2, y2].
        """
        # Ensure the area + velocity term doesn't go negative
        if (self.x[6] + self.x[2]) <= 0:
            self.x[6] *= 0.0

        # Predict step
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q

        self.age += 1
        if self.time_since_update > 0:
            self.hit_streak = 0
        self.time_since_update += 1
        self.history.append(convert_x_to_bbox(self.x))
        return self.history[-1]

    def get_state(self):
        """
        Returns:
            Current estimated bounding box [x1, y1, x2, y2].
        """
        return convert_x_to_bbox(self.x)


class SortTracker:
    """
    SORT: Simple Online and Realtime Tracking.

    Maintains a set of KalmanBoxTracker instances and performs
    detection-to-track association using the Hungarian algorithm
    on an IoU cost matrix. Handles track creation and deletion.

    This implementation follows the original SORT paper:
    "Simple Online and Realtime Tracking" (Bewley et al., 2016).
    """

    def __init__(self, max_age=30, min_hits=3, iou_threshold=0.3):
        """
        Args:
            max_age: Maximum frames to keep a track alive without
                     a detection match (handles short occlusions).
            min_hits: Minimum consecutive detection matches before a
                      track is considered confirmed and reported.
            iou_threshold: Minimum IoU for a valid match between
                           a detection and a track prediction.
        """
        self.max_age = max_age
        self.min_hits = min_hits
        self.iou_threshold = iou_threshold
        self.tracks = []
        self.frame_count = 0

    def update(self, dets):
        """
        Update the tracker with a new set of detections.

        Association pipeline:
        1. Predict all existing tracks forward one frame.
        2. Compute IoU cost matrix between detections and track predictions.
        3. Run Hungarian algorithm to find optimal assignment.
        4. Update matched tracks, create new tracks for unmatched detections,
           remove stale tracks.

        Args:
            dets: (N, 5) numpy array -- each row [x1, y1, x2, y2, score].
                  Empty array if no detections in this frame.

        Returns:
            List of active, confirmed tracks:
            [(track_id, [x1, y1, x2, y2], dwell_time, first_seen, last_seen), ...]
        """
        self.frame_count += 1

        # --- Step 1: Predict all existing tracks ---
        for track in self.tracks:
            track.predict()

        # --- Step 2: Build track state matrix ---
        # NOTE: fixed original bug — this matrix holds [x1, y1, x2, y2] only
        # (get_state() returns 4 values); the extra column crashed the camera
        # thread with a broadcast ValueError as soon as any track existed.
        trks = np.zeros((len(self.tracks), 4))
        for i, track in enumerate(self.tracks):
            state = track.get_state().flatten()
            trks[i, :] = state

        # --- Step 3: Association ---
        matched, unmatched_dets, unmatched_trks = self._associate(dets, trks)

        # --- Step 4a: Update matched tracks ---
        for t, d in matched:
            self.tracks[t].update(dets[d, :4])

        # --- Step 4b: Create new tracks for unmatched detections ---
        for i in unmatched_dets:
            track = KalmanBoxTracker(dets[i, :4])
            self.tracks.append(track)

        # --- Step 4c: Remove stale / unconfirmed tracks ---
        i = len(self.tracks)
        for trk in reversed(self.tracks):
            i -= 1
            # Remove if unmatched for too long (left frame or fully occluded)
            if trk.time_since_update > self.max_age:
                self.tracks.pop(i)
            # Remove unconfirmed tracks that haven't been updated recently
            elif trk.hits < self.min_hits and self.frame_count > self.min_hits:
                if trk.time_since_update > self.max_age // 2:
                    self.tracks.pop(i)

        # --- Build output: only confirmed tracks with dwell info ---
        active = []
        for track in self.tracks:
            # Only report tracks that have met the minimum hit threshold
            if track.hit_streak >= self.min_hits or track.hits >= self.min_hits:
                state = track.get_state().flatten()
                bbox = [int(max(0, state[0])), int(max(0, state[1])),
                        int(max(0, state[2])), int(max(0, state[3]))]
                dwell_time = time.time() - track.first_seen
                active.append((track.id, bbox, dwell_time,
                               track.first_seen, track.last_seen))

        return active

    def _associate(self, detections, tracks):
        """
        Associate detections with tracks via Hungarian algorithm on IoU.

        Returns:
            matched: (M, 2) array of (track_idx, detection_idx) pairs.
            unmatched_dets: array of unmatched detection indices.
            unmatched_trks: array of unmatched track indices.
        """
        if len(tracks) == 0:
            return (np.empty((0, 2), dtype=int),
                    np.arange(len(detections)),
                    np.empty((0,), dtype=int))

        if len(detections) == 0:
            return (np.empty((0, 2), dtype=int),
                    np.empty((0,), dtype=int),
                    np.arange(len(tracks)))

        # IoU matrix: rows = detections, columns = tracks.
        iou_matrix = iou_batch(detections[:, :4], tracks[:, :4])

        # Hungarian: minimize cost (we negate IoU to maximize it).
        # linear_sum_assignment returns (row_idx, col_idx) = (det_idx, trk_idx).
        det_idx, trk_idx = linear_sum_assignment(-iou_matrix)

        # NOTE: fixed original bug — the row/column indices were swapped,
        # corrupting the matched pairs and the unmatched sets (only worked
        # by accident with exactly one object in frame).
        matched = []
        unmatched_dets = set(range(len(detections)))
        unmatched_trks = set(range(len(tracks)))
        for d, t in zip(det_idx, trk_idx):
            if iou_matrix[d, t] >= self.iou_threshold:
                matched.append([t, d])   # (track_idx, detection_idx)
                unmatched_dets.discard(d)
                unmatched_trks.discard(t)

        return (np.array(matched, dtype=int).reshape(-1, 2),
                np.array(sorted(unmatched_dets), dtype=int),
                np.array(sorted(unmatched_trks), dtype=int))


class DetectionProcessor:
    """
    High-level processor combining ONNX YOLOv8 detection with SORT tracking.

    Pipeline: frame -> letterbox -> ONNX Runtime inference -> class-aware
    NMS -> letterbox-inverse coordinate mapping -> SORT tracking ->
    dwell time extraction.

    Replaces the previous ultralytics/PyTorch backend with onnxruntime,
    which shrinks the packaged application from >1 GB to a few hundred MB
    and removes the torch dependency entirely.
    """

    #: gray value used to pad the letterbox canvas (same as ultralytics)
    LETTERBOX_FILL = 114
    #: offset used to separate classes during NMS (same trick as ultralytics)
    NMS_CLASS_OFFSET = 7680.0

    def __init__(self, model_path="yolov8n.onnx", conf_threshold=0.5,
                 device="cpu", iou_threshold=0.45, max_det=300):
        """
        Args:
            model_path: Path to the exported YOLOv8 ONNX model.
            conf_threshold: Detection confidence threshold.
            device: "cpu" or "cuda" (falls back to CPU when the CUDA
                    execution provider is unavailable, e.g. with the
                    plain `onnxruntime` CPU build).
            iou_threshold: IoU threshold for non-maximum suppression.
            max_det: Maximum number of detections kept per frame.
        """
        self.conf_threshold = float(conf_threshold)
        self.iou_threshold = float(iou_threshold)
        self.max_det = int(max_det)
        self.device = device

        # --- ONNX Runtime session -------------------------------------
        wanted = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                  if device == "cuda" else ["CPUExecutionProvider"])
        available = ort.get_available_providers()
        providers = [p for p in wanted if p in available] or ["CPUExecutionProvider"]
        self.session = ort.InferenceSession(str(model_path), providers=providers)

        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

        # Static input size (usually [1, 3, 640, 640]); tolerate dynamic
        # axes by falling back to 640.
        shape = self.session.get_inputs()[0].shape
        try:
            self.input_size = int(shape[-1]) if isinstance(shape[-1], int) else 640
        except (TypeError, ValueError):
            self.input_size = 640

        # --- SORT tracker (unchanged behaviour) ------------------------
        self.tracker = SortTracker()
        self.frame_count = 0

    # ------------------------------------------------------------------
    # Preprocessing
    # ------------------------------------------------------------------
    def _letterbox(self, frame):
        """
        Resize + pad the frame to (size, size) preserving aspect ratio.

        Returns:
            (canvas, scale, pad_x, pad_y) where `scale` maps net-space
            coordinates back to frame coordinates:
                x_frame = (x_net - pad_x) / scale
                y_frame = (y_net - pad_y) / scale
        """
        h, w = frame.shape[:2]
        size = self.input_size
        scale = min(size / h, size / w)
        new_w, new_h = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        canvas = np.full((size, size, 3), self.LETTERBOX_FILL, dtype=np.uint8)
        pad_x = (size - new_w) // 2
        pad_y = (size - new_h) // 2
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
        return canvas, scale, pad_x, pad_y

    # ------------------------------------------------------------------
    # Postprocessing
    # ------------------------------------------------------------------
    @staticmethod
    def _nms(boxes_xyxy, scores, iou_threshold):
        """
        Vectorized non-maximum suppression.

        Args:
            boxes_xyxy: (N, 4) array of [x1, y1, x2, y2].
            scores: (N,) confidence scores.
            iou_threshold: overlap threshold for suppression.

        Returns:
            Array of indices of boxes to keep (highest score first).
        """
        x1, y1 = boxes_xyxy[:, 0], boxes_xyxy[:, 1]
        x2, y2 = boxes_xyxy[:, 2], boxes_xyxy[:, 3]
        areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)

        order = scores.argsort()[::-1]
        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)
            if order.size == 1:
                break
            rest = order[1:]
            xx1 = np.maximum(x1[i], x1[rest])
            yy1 = np.maximum(y1[i], y1[rest])
            xx2 = np.minimum(x2[i], x2[rest])
            yy2 = np.minimum(y2[i], y2[rest])
            inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
            iou = inter / np.maximum(areas[i] + areas[rest] - inter, 1e-12)
            order = rest[iou <= iou_threshold]
        return np.array(keep, dtype=int)

    def _postprocess(self, output, scale, pad_x, pad_y, frame_w, frame_h):
        """
        Parse the raw (1, 84, 8400) YOLOv8 output into frame-space
        detections.

        Returns:
            (N, 5) numpy array of [x1, y1, x2, y2, score] in frame
            pixel coordinates (empty array when nothing is detected).
        """
        pred = np.asarray(output)
        if pred.ndim == 3:      # (1, 84, 8400) -> (84, 8400)
            pred = pred[0]

        boxes = pred[:4, :].T              # (8400, 4) cx, cy, w, h
        class_scores = pred[4:, :]          # (80, 8400)
        cls_ids = class_scores.argmax(axis=0)
        confs = class_scores.max(axis=0)

        keep = confs > self.conf_threshold
        if not np.any(keep):
            return np.empty((0, 5), dtype=np.float32)

        boxes, confs, cls_ids = boxes[keep], confs[keep], cls_ids[keep]

        # cxcywh -> xyxy (net space)
        xyxy = np.concatenate(
            [boxes[:, :2] - boxes[:, 2:] / 2.0,
             boxes[:, :2] + boxes[:, 2:] / 2.0],
            axis=1,
        )

        # Class-aware NMS: offset boxes by class so different classes
        # never suppress each other (same approach as ultralytics).
        offset = cls_ids.astype(np.float64)[:, None] * self.NMS_CLASS_OFFSET
        keep_idx = self._nms(xyxy + offset, confs, self.iou_threshold)
        xyxy, confs = xyxy[keep_idx], confs[keep_idx]

        # Cap the number of detections (ultralytics default: 300).
        if len(confs) > self.max_det:
            top = np.argsort(-confs)[: self.max_det]
            xyxy, confs = xyxy[top], confs[top]

        # Undo the letterbox: net space -> original frame space.
        xyxy[:, [0, 2]] = (xyxy[:, [0, 2]] - pad_x) / scale
        xyxy[:, [1, 3]] = (xyxy[:, [1, 3]] - pad_y) / scale
        xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, frame_w)
        xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, frame_h)

        return np.concatenate([xyxy, confs[:, None].astype(np.float32)], axis=1)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def process_frame(self, frame):
        """
        Run detection + tracking on a single video frame.

        Args:
            frame: numpy array (H, W, 3) in BGR format (OpenCV default).

        Returns:
            tracked_objects: list of dicts with keys:
                - id: unique track ID
                - bbox: [x1, y1, x2, y2]
                - dwell_time: seconds since first appearance
                - first_seen: epoch timestamp of first appearance
                - last_seen: epoch timestamp of most recent detection
        """
        self.frame_count += 1
        frame_h, frame_w = frame.shape[:2]

        # 1. Preprocess: letterbox to (size, size), BGR->RGB, /255, CHW, batch
        canvas, scale, pad_x, pad_y = self._letterbox(frame)
        blob = canvas[..., ::-1].astype(np.float32) / 255.0   # BGR -> RGB
        blob = blob.transpose(2, 0, 1)[None]                   # (1, 3, H, W)

        # 2. ONNX inference
        output = self.session.run([self.output_name], {self.input_name: blob})[0]

        # 3. Postprocess: parse output, NMS, map coordinates to frame space
        dets = self._postprocess(output, scale, pad_x, pad_y, frame_w, frame_h)

        # 4. SORT tracking
        active_tracks = self.tracker.update(dets)

        # 5. Format output
        tracked_objects = []
        for track_id, bbox, dwell_time, first_seen, last_seen in active_tracks:
            tracked_objects.append({
                "id": track_id,
                "bbox": bbox,
                "dwell_time": dwell_time,
                "first_seen": first_seen,
                "last_seen": last_seen,
            })

        return tracked_objects
