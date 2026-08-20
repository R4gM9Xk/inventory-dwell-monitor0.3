/**
 * Dwell Time Monitor - Frontend Application
 * ==========================================
 *
 * Connection flow:
 * 1. Fetch camera list from /api/cameras, build camera tabs.
 * 2. Connect to WebSocket at /ws
 * 3. Receive JSON array of tracked objects from ALL cameras every ~1 second.
 * 4. Update the objects grid: create, update, or remove cards.
 * 5. Each card shows camera label, thumbnail, ID, dwell time (D:HH:MM), and alert badge.
 * 6. Two view modes: single (tab-switch) or split (all cameras side-by-side).
 * 7. Dwell times are computed locally from first_seen timestamps for
 *    smooth updates between server pushes.
 */

(function () {
    "use strict";

    // ---- Configuration ----
    const WS_URL = `ws://${location.host}/ws`;
    const RECONNECT_DELAY_MS = 3000;
    const DWELL_UPDATE_INTERVAL_MS = 60000; // 1-minute tick for dwell display

    // ---- DOM References ----
    const grid = document.getElementById("objectsGrid");
    const emptyState = document.getElementById("emptyState");
    const connectionStatus = document.getElementById("connectionStatus");
    const objectCount = document.getElementById("objectCount");
    const videoContainer = document.getElementById("videoContainer");
    const videoOverlay = document.getElementById("videoOverlay");
    const fpsIndicator = document.getElementById("fpsIndicator");
    const kpiCameras = document.getElementById("kpiCameras");
    const kpiTracked = document.getElementById("kpiTracked");
    const kpiAvg = document.getElementById("kpiAvg");
    const kpiMax = document.getElementById("kpiMax");
    const kpiAlerts = document.getElementById("kpiAlerts");
    const footDot = document.getElementById("footDot");
    const footStatusText = document.getElementById("footStatusText");
    const footClock = document.getElementById("footClock");
    const emptyAddCamera = document.getElementById("emptyAddCamera");
    const cameraTabs = document.getElementById("cameraTabs");
    const cameraFilterTabs = document.getElementById("cameraFilterTabs");
    const viewModeBtn = document.getElementById("viewModeBtn");

    // ---- State ----
    let trackedObjects = {};         // compositeKey -> obj
    let cardElements = {};           // compositeKey -> HTMLElement
    let cameras = [];                // [{camera_id, source, active}, ...]
    let selectedCamera = "all";      // "all" or camera_id (number)
    let viewMode = "single";         // "single" or "split"
    let ws = null;
    let reconnectTimer = null;
    let dwellTimer = null;
    let fpsTimer = null;
    let isConnected = false;
    let lastMessageAt = 0;

    // Alert thresholds (seconds), kept in sync with the backend /api/config.
    let alertGreenMax = 259200;      // < 3 days
    let alertYellowMax = 518400;     // 3-6 days

    // ---- Camera Management ----
    function fetchCameras() {
        fetch("/api/cameras")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                cameras = data;
                if (kpiCameras) kpiCameras.textContent = cameras.length;
                buildCameraTabs();
                buildCameraFilterTabs();
                renderCameraList();
                refreshVideoFeedIfNeeded();
                if (viewMode === "split") updateVideoView();
            })
            .catch(function () {});
    }

    function buildCameraTabs() {
        cameraTabs.innerHTML = "";
        cameras.forEach(function (cam) {
            var tab = document.createElement("button");
            tab.className = "camera-tab";
            if (cam.camera_id === selectedCamera || (selectedCamera === "all" && cam.camera_id === 0)) {
                tab.classList.add("active");
            }
            tab.dataset.cameraId = cam.camera_id;
            tab.textContent = "Camera " + (cam.camera_id + 1);
            tab.title = "Source: " + cam.source;
            tab.addEventListener("click", function () {
                if (viewMode === "single") switchCamera(cam.camera_id);
            });
            cameraTabs.appendChild(tab);
        });
    }

    function buildCameraFilterTabs() {
        cameraFilterTabs.innerHTML = "";
        var allTab = document.createElement("button");
        allTab.className = "camera-filter-tab";
        if (selectedCamera === "all") allTab.classList.add("active");
        allTab.textContent = "All";
        allTab.addEventListener("click", function () { switchCamera("all"); });
        cameraFilterTabs.appendChild(allTab);

        cameras.forEach(function (cam) {
            var tab = document.createElement("button");
            tab.className = "camera-filter-tab";
            if (cam.camera_id === selectedCamera) tab.classList.add("active");
            tab.dataset.cameraId = cam.camera_id;
            tab.textContent = "Cam " + (cam.camera_id + 1);
            tab.addEventListener("click", function () { switchCamera(cam.camera_id); });
            cameraFilterTabs.appendChild(tab);
        });
    }

    function switchCamera(cameraId) {
        if (cameraId === selectedCamera) return;
        selectedCamera = cameraId;

        if (viewMode === "single") {
            if (cameraId === "all") {
                var target = cameras.length > 0 ? cameras[0].camera_id : 0;
                document.getElementById("videoFeed").src = "/video_feed/" + target;
            } else {
                document.getElementById("videoFeed").src = "/video_feed/" + cameraId;
            }
        }

        // Update camera tab active states (only in single mode)
        var tabs = cameraTabs.querySelectorAll(".camera-tab");
        tabs.forEach(function (tab) {
            var isActive = parseInt(tab.dataset.cameraId, 10) === cameraId;
            tab.classList.toggle("active", isActive);
        });

        // Update filter tab active states
        var filterTabs = cameraFilterTabs.querySelectorAll(".camera-filter-tab");
        filterTabs.forEach(function (tab) {
            if (tab.dataset.cameraId !== undefined) {
                tab.classList.toggle("active", parseInt(tab.dataset.cameraId, 10) === cameraId);
            } else {
                tab.classList.toggle("active", cameraId === "all");
            }
        });

        rebuildGrid();
    }

    // ---- View Mode: Single / Split ----
    function toggleViewMode() {
        if (cameras.length <= 1) return; // No point in split with single camera

        viewMode = (viewMode === "single") ? "split" : "single";
        viewModeBtn.textContent = (viewMode === "split") ? "⊟ Single" : "⊞ Split";
        viewModeBtn.classList.toggle("active", viewMode === "split");

        // Show/hide camera tabs
        cameraTabs.style.display = (viewMode === "split") ? "none" : "flex";

        updateVideoView();
        rebuildGrid();
    }

    function updateVideoView() {
        if (viewMode === "split") {
            buildSplitView();
        } else {
            buildSingleView();
        }
    }

    function buildSplitView() {
        // Replace video container content with a grid of all camera feeds
        videoContainer.innerHTML = "";
        videoContainer.className = "video-container split-mode";

        var gridWrapper = document.createElement("div");
        gridWrapper.className = "split-video-grid";

        cameras.forEach(function (cam) {
            var item = document.createElement("div");
            item.className = "split-video-item";

            var img = document.createElement("img");
            img.className = "split-video-feed";
            img.src = "/video_feed/" + cam.camera_id;
            img.alt = "Camera " + (cam.camera_id + 1);
            img.loading = "lazy";

            var label = document.createElement("div");
            label.className = "split-video-label";
            label.textContent = "Camera " + (cam.camera_id + 1);

            item.appendChild(img);
            item.appendChild(label);
            gridWrapper.appendChild(item);
        });

        videoContainer.appendChild(gridWrapper);
    }

    function buildSingleView() {
        videoContainer.innerHTML = "";
        videoContainer.className = "video-container";

        var img = document.createElement("img");
        img.id = "videoFeed";
        var target = cameras.length > 0 ? cameras[0].camera_id : 0;
        img.src = "/video_feed/" + target;
        img.alt = "Live Video Feed";
        videoContainer.appendChild(img);

        var overlay = document.createElement("div");
        overlay.className = "video-overlay hidden";
        overlay.id = "videoOverlay";
        overlay.innerHTML = "<span>Connecting to camera...</span>";
        videoContainer.appendChild(overlay);

        // Re-bind event listeners
        img.addEventListener("load", function () {
            overlay.classList.add("hidden");
        });
        img.addEventListener("error", function () {
            overlay.classList.remove("hidden");
            overlay.innerHTML = "<span>Video stream unavailable. Check camera connection.</span>";
        });
    }

    // ---- WebSocket Connection ----
    function connect() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

        setConnectionState("connecting");

        try {
            ws = new WebSocket(WS_URL);
        } catch (e) {
            console.error("[WS] Connection error:", e);
            scheduleReconnect();
            return;
        }

        ws.onopen = function () {
            console.log("[WS] Connected");
            setConnectionState("connected");
            isConnected = true;
            var overlay = document.getElementById("videoOverlay");
            if (overlay) overlay.classList.add("hidden");
            startDwellTimer();
            startFpsTimer();
            if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        };

        ws.onmessage = function (event) {
            try {
                const data = JSON.parse(event.data);
                lastMessageAt = Date.now();
                onTrackingUpdate(data);
                updateFpsIndicator();
            } catch (e) {
                console.error("[WS] Parse error:", e);
            }
        };

        ws.onclose = function () {
            console.log("[WS] Disconnected");
            setConnectionState("disconnected");
            isConnected = false;
            stopDwellTimer();
            stopFpsTimer();
            lastMessageAt = 0;
            scheduleReconnect();
        };

        ws.onerror = function () {};
    }

    function scheduleReconnect() {
        if (reconnectTimer) return;
        reconnectTimer = setTimeout(function () {
            reconnectTimer = null;
            connect();
            fetchCameras();
        }, RECONNECT_DELAY_MS);
    }

    function setConnectionState(state) {
        connectionStatus.textContent =
            state === "connected" ? "Connected"
            : state === "connecting" ? "Connecting..."
            : "Disconnected";
        connectionStatus.className = "connection-status " + state;
        if (footStatusText) {
            footStatusText.textContent =
                state === "connected" ? "已连接"
                : state === "connecting" ? "连接中..."
                : "未连接";
        }
        if (footDot) footDot.className = "foot-dot " + state;
    }

    // ---- Dwell Timer ----
    function startDwellTimer() {
        if (dwellTimer) return;
        dwellTimer = setInterval(function () {
            updateDwellTimes();
            fetchStats();
        }, DWELL_UPDATE_INTERVAL_MS);
    }

    function stopDwellTimer() {
        if (dwellTimer) { clearInterval(dwellTimer); dwellTimer = null; }
    }

    // ---- Composite Key ----
    function makeKey(cameraId, objectId) {
        return cameraId + "-" + objectId;
    }

    // ---- Tracking Data Handler ----
    function onTrackingUpdate(data) {
        if (!Array.isArray(data)) return;

        const incomingKeys = new Set();
        var selectedCamId = selectedCamera;

        for (const obj of data) {
            const camId = obj.camera_id !== undefined ? obj.camera_id : 0;
            const objId = obj.id;
            const key = makeKey(camId, objId);
            incomingKeys.add(key);

            if (trackedObjects[key]) {
                Object.assign(trackedObjects[key], obj);
                updateCard(key, trackedObjects[key]);
            } else {
                trackedObjects[key] = obj;
                if (viewMode === "split" || selectedCamId === "all" || camId === selectedCamId) {
                    createCard(key, obj);
                }
            }
        }

        var existingKeys = Object.keys(trackedObjects);
        for (const key of existingKeys) {
            if (!incomingKeys.has(key)) {
                removeCard(key);
                delete trackedObjects[key];
            }
        }

        updateObjectCount();
        var visibleCards = grid.querySelectorAll(".object-card:not(.removing)");
        emptyState.style.display = visibleCards.length === 0 ? "flex" : "none";
    }

    function updateObjectCount() {
        var selectedCamId = selectedCamera;
        var count = 0;
        var alerts = 0;
        for (var key in trackedObjects) {
            var obj = trackedObjects[key];
            var visible = viewMode === "split" || selectedCamId === "all" || obj.camera_id === selectedCamId;
            if (visible) {
                count++;
                if (obj.alert_class === "red") alerts++;
            }
        }
        objectCount.textContent = count + " object" + (count !== 1 ? "s" : "") + " tracked";
        if (kpiTracked) kpiTracked.textContent = count;
        if (kpiAlerts) kpiAlerts.textContent = alerts;
    }

    // ---- Card Management ----
    function createCard(key, obj) {
        emptyState.style.display = "none";

        const card = document.createElement("div");
        card.className = "object-card";
        card.dataset.key = key;

        const thumb = document.createElement("img");
        thumb.className = "thumbnail";
        thumb.alt = "Object " + obj.id;
        thumb.src = obj.thumbnail || "";
        thumb.onerror = function () {
            thumb.src = "";
            thumb.style.background = "#333";
            thumb.style.display = "block";
        };

        const body = document.createElement("div");
        body.className = "card-body";

        var camId = obj.camera_id !== undefined ? obj.camera_id : 0;
        if (cameras.length > 1 || camId !== 0) {
            const camEl = document.createElement("div");
            camEl.className = "card-camera";
            camEl.textContent = "Camera " + (camId + 1);
            body.appendChild(camEl);
        }

        const idEl = document.createElement("div");
        idEl.className = "card-id";
        idEl.textContent = "ID: " + obj.id;

        const dwellEl = document.createElement("div");
        dwellEl.className = "card-dwell";
        dwellEl.id = "dwell-" + key;
        dwellEl.textContent = formatDwell(obj.dwell_time || 0);

        const statusEl = document.createElement("div");
        statusEl.className = "card-status";

        body.appendChild(idEl);
        body.appendChild(dwellEl);
        body.appendChild(statusEl);
        card.appendChild(thumb);
        card.appendChild(body);

        var inserted = false;
        var children = grid.children;
        for (var i = 0; i < children.length; i++) {
            var child = children[i];
            if (child.classList.contains("object-card") && child.dataset.key > key) {
                grid.insertBefore(card, child);
                inserted = true;
                break;
            }
        }
        if (!inserted) grid.appendChild(card);

        cardElements[key] = card;
        updateCard(key, obj);
    }

    function updateCard(key, obj) {
        const card = cardElements[key];
        if (!card) return;

        const thumb = card.querySelector(".thumbnail");
        if (thumb && obj.thumbnail) thumb.src = obj.thumbnail;

        const dwellEl = card.querySelector(".card-dwell");
        if (dwellEl) dwellEl.textContent = formatDwell(obj.dwell_time || 0);

        const alertClass = obj.alert_class || "green";
        card.className = "object-card alert-" + alertClass;

        const statusEl = card.querySelector(".card-status");
        if (statusEl) {
            statusEl.className = "card-status " + alertClass;
            statusEl.textContent = alertClass === "green" ? "Normal"
                : alertClass === "yellow" ? "Watch" : "Alert";
        }
        if (thumb) thumb.style.borderBottom = "2px solid var(--" + alertClass + ")";
    }

    function removeCard(key) {
        const card = cardElements[key];
        if (!card) return;
        card.classList.add("removing");
        setTimeout(function () {
            if (card.parentNode) card.parentNode.removeChild(card);
            delete cardElements[key];
        }, 200);
    }

    // ---- Rebuild Grid ----
    function rebuildGrid() {
        for (var key in cardElements) {
            var card = cardElements[key];
            if (card && card.parentNode) card.parentNode.removeChild(card);
        }
        cardElements = {};

        var selectedCamId = selectedCamera;
        var keys = Object.keys(trackedObjects).sort();
        for (var i = 0; i < keys.length; i++) {
            var key = keys[i];
            var obj = trackedObjects[key];
            if (viewMode === "split" || selectedCamId === "all" || obj.camera_id === selectedCamId) {
                createCard(key, obj);
            }
        }

        updateObjectCount();
        var visibleCards = grid.querySelectorAll(".object-card:not(.removing)");
        emptyState.style.display = visibleCards.length === 0 ? "flex" : "none";
    }

    // ---- Dwell Time Update ----
    function updateDwellTimes() {
        const now = Date.now() / 1000;

        for (const key in trackedObjects) {
            const obj = trackedObjects[key];
            if (!obj.first_seen) continue;

            const dwell = now - obj.first_seen;
            obj.dwell_time = dwell;

            const dwellEl = document.getElementById("dwell-" + key);
            if (dwellEl) dwellEl.textContent = formatDwell(dwell);

            const newClass = dwell < alertGreenMax ? "green"
                : dwell < alertYellowMax ? "yellow"
                : "red";

            if (newClass !== obj.alert_class) {
                obj.alert_class = newClass;
                const card = cardElements[key];
                if (card) {
                    card.className = "object-card alert-" + newClass;
                    const statusEl = card.querySelector(".card-status");
                    if (statusEl) {
                        statusEl.className = "card-status " + newClass;
                        statusEl.textContent = newClass === "green" ? "Normal"
                            : newClass === "yellow" ? "Watch" : "Alert";
                    }
                    const thumb = card.querySelector(".thumbnail");
                    if (thumb) thumb.style.borderBottom = "2px solid var(--" + newClass + ")";
                }
            }
        }
    }

    // ---- Formatting ----
    function formatDwell(seconds) {
        const s = Math.max(0, Math.floor(seconds));
        const d = Math.floor(s / 86400);
        const h = Math.floor((s % 86400) / 3600);
        const m = Math.floor((s % 3600) / 60);
        return d + ":" + String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
    }

    // ---- FPS Indicator ----
    function startFpsTimer() {
        if (fpsTimer) return;
        fpsTimer = setInterval(updateFpsIndicator, 2000);
    }

    function stopFpsTimer() {
        if (fpsTimer) { clearInterval(fpsTimer); fpsTimer = null; }
    }

    function updateFpsIndicator() {
        var elapsed = lastMessageAt ? (Date.now() - lastMessageAt) / 1000 : Infinity;
        if (elapsed < 5) {
            fpsIndicator.textContent = "Receiving updates";
            fpsIndicator.style.color = "var(--green)";
        } else {
            fpsIndicator.textContent = "Waiting for data...";
            fpsIndicator.style.color = "var(--yellow)";
        }
    }

    // ---- Stats ----
    function fetchStats() {
        fetch("/api/stats")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (kpiAvg) kpiAvg.textContent = formatDuration(data.avg_dwell || 0);
                if (kpiMax) kpiMax.textContent = formatDuration(data.max_dwell || 0);
                if (kpiCameras) kpiCameras.textContent = data.camera_count || cameras.length || 0;
            })
            .catch(function () {});
    }

    function formatDuration(seconds) {
        if (!seconds || seconds <= 0) return "N/A";
        const d = Math.floor(seconds / 86400);
        const h = Math.floor((seconds % 86400) / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        if (d > 0) return d + "d " + h + "h " + m + "m";
        if (h > 0) return h + "h " + m + "m";
        if (m > 0) return m + "m";
        return Math.floor(seconds) + "s";
    }

    // ---- Camera Management (Settings Modal) ----
    function renderCameraList() {
        var list = document.getElementById("cameraList");
        if (!list) return;
        list.innerHTML = "";

        if (cameras.length === 0) {
            var empty = document.createElement("p");
            empty.className = "camera-list-empty";
            empty.textContent = "No cameras configured. Add one below.";
            list.appendChild(empty);
            return;
        }

        cameras.forEach(function (cam) {
            var row = document.createElement("div");
            row.className = "camera-row";

            var name = document.createElement("span");
            name.className = "cam-name";
            name.textContent = "Camera " + (cam.camera_id + 1);

            var source = document.createElement("span");
            source.className = "cam-source";
            source.title = cam.source;
            source.textContent = cam.source;

            var status = document.createElement("span");
            status.className = "cam-status " + (cam.active ? "active" : "inactive");
            status.textContent = cam.active ? "Running" : "Stopped";

            var del = document.createElement("button");
            del.className = "cam-delete";
            del.textContent = "Remove";
            del.title = "Stop and remove this camera";
            del.addEventListener("click", function () {
                removeCamera(cam.camera_id);
            });

            row.appendChild(name);
            row.appendChild(source);
            row.appendChild(status);
            row.appendChild(del);
            list.appendChild(row);
        });
    }

    function refreshVideoFeedIfNeeded() {
        if (viewMode !== "single") return;
        var feed = document.getElementById("videoFeed");
        if (!feed) return;
        var parts = feed.src.split("/video_feed/");
        var currentId = parts.length > 1 ? parseInt(parts[1], 10) : -1;
        var exists = cameras.some(function (c) { return c.camera_id === currentId; });
        if (!exists && cameras.length > 0) {
            var target = selectedCamera;
            if (target === "all" || !cameras.some(function (c) { return c.camera_id === target; })) {
                target = cameras[0].camera_id;
            }
            feed.src = "/video_feed/" + target;
        }
    }

    function addCamera() {
        var input = document.getElementById("cameraSourceInput");
        var source = (input.value || "").trim();
        if (!source) {
            showSettingsStatus("Please enter an RTSP URL or webcam index.", "err");
            input.focus();
            return;
        }
        fetch("/api/cameras", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ source: source }),
        })
            .then(function (r) {
                return r.json().then(function (data) { return { ok: r.ok, data: data }; });
            })
            .then(function (res) {
                if (res.ok) {
                    input.value = "";
                    showSettingsStatus("Camera added successfully.", "ok");
                    fetchCameras();
                } else {
                    showSettingsStatus(res.data.error || "Failed to add camera.", "err");
                }
            })
            .catch(function () {
                showSettingsStatus("Network error while adding camera.", "err");
            });
    }

    function removeCamera(cameraId) {
        if (!window.confirm("Remove Camera " + (cameraId + 1) + "? Its stream will be stopped.")) return;
        fetch("/api/cameras/" + cameraId, { method: "DELETE" })
            .then(function (r) {
                return r.json().then(function (data) { return { ok: r.ok, data: data }; });
            })
            .then(function (res) {
                if (res.ok) {
                    showSettingsStatus("Camera removed.", "ok");
                    fetchCameras();
                } else {
                    showSettingsStatus(res.data.error || "Failed to remove camera.", "err");
                }
            })
            .catch(function () {
                showSettingsStatus("Network error while removing camera.", "err");
            });
    }

    function showSettingsStatus(msg, type) {
        var statusEl = document.getElementById("settingsStatus");
        if (!statusEl) return;
        statusEl.textContent = msg;
        statusEl.className = "settings-status " + (type === "ok" ? "ok" : "err");
    }

    // ---- Settings Modal ----
    function openSettings() {
        var modal = document.getElementById("settingsModal");
        if (modal) modal.classList.remove("hidden");
        renderCameraList();
        loadConfig();
        pollScanStatus();
        showSettingsStatus("", "ok");
    }

    function closeSettings() {
        var modal = document.getElementById("settingsModal");
        if (modal) modal.classList.add("hidden");
    }

    function secondsToHours(seconds) {
        return Math.round((seconds / 3600) * 100) / 100;
    }

    function loadConfig() {
        fetch("/api/config")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (typeof data.alert_green_max === "number") alertGreenMax = data.alert_green_max;
                if (typeof data.alert_yellow_max === "number") alertYellowMax = data.alert_yellow_max;
                var g = document.getElementById("greenMaxInput");
                var y = document.getElementById("yellowMaxInput");
                if (g) g.value = secondsToHours(alertGreenMax);
                if (y) y.value = secondsToHours(alertYellowMax);
            })
            .catch(function () {});
    }

    function applyThresholds() {
        // UI works in hours; the backend stores seconds.
        var gH = parseFloat(document.getElementById("greenMaxInput").value);
        var yH = parseFloat(document.getElementById("yellowMaxInput").value);
        if (!gH || gH <= 0 || !yH || yH <= 0) {
            showSettingsStatus("Thresholds must be positive numbers (hours).", "err");
            return;
        }
        if (yH <= gH) {
            showSettingsStatus("Yellow threshold must be greater than Green threshold.", "err");
            return;
        }
        fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                alert_green_max: Math.round(gH * 3600),
                alert_yellow_max: Math.round(yH * 3600),
            }),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                alertGreenMax = data.alert_green_max;
                alertYellowMax = data.alert_yellow_max;
                updateDwellTimes();
                showSettingsStatus("Thresholds applied (in hours).", "ok");
            })
            .catch(function () {
                showSettingsStatus("Network error while applying thresholds.", "err");
            });
    }

    // ---- RTSP Network Scanner ----
    var scanPollTimer = null;
    var scanAddedUrls = {};      // url -> true, keeps "Added" state across polls

    function parseScanPorts() {
        var raw = document.getElementById("scanPortsInput").value.trim();
        if (!raw) return null;   // backend defaults
        var parts = raw.split(",");
        var ports = [];
        for (var i = 0; i < parts.length; i++) {
            var p = parseInt(parts[i].trim(), 10);
            if (isNaN(p) || p < 1 || p > 65535) return false;
            if (ports.indexOf(p) === -1) ports.push(p);
        }
        return ports.length ? ports : false;
    }

    function startScan() {
        // Blank target = auto-detect local LAN(s); blank ports = common defaults.
        var target = document.getElementById("scanTargetInput").value.trim();
        var ports = parseScanPorts();
        if (ports === false) {
            showSettingsStatus("Invalid port list (e.g. 554,8554).", "err");
            return;
        }
        var username = document.getElementById("scanUsernameInput").value.trim();
        var password = document.getElementById("scanPasswordInput").value;
        var payload = { target: target, ports: ports };
        if (username) {
            payload.username = username;
            payload.password = password;
        }
        fetch("/api/scan/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                return r.json().then(function (data) { return { ok: r.ok, data: data }; });
            })
            .then(function (res) {
                if (res.ok) {
                    scanAddedUrls = {};
                    showSettingsStatus(res.data.message || "Scan started.", "ok");
                    startScanPolling();
                    pollScanStatus();
                } else {
                    showSettingsStatus(res.data.error || "Failed to start scan.", "err");
                    pollScanStatus();
                }
            })
            .catch(function () {
                showSettingsStatus("Network error while starting scan.", "err");
            });
    }

    function stopScan() {
        fetch("/api/scan/stop", { method: "POST" })
            .then(function () { pollScanStatus(); })
            .catch(function () {});
    }

    function startScanPolling() {
        if (scanPollTimer) return;
        scanPollTimer = setInterval(pollScanStatus, 1000);
    }

    function stopScanPolling() {
        if (scanPollTimer) { clearInterval(scanPollTimer); scanPollTimer = null; }
    }

    function pollScanStatus() {
        fetch("/api/scan/status")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                renderScanStatus(data);
                if (data.running) {
                    startScanPolling();
                } else {
                    stopScanPolling();
                }
            })
            .catch(function () {});
    }

    function renderScanStatus(data) {
        var startBtn = document.getElementById("scanStartBtn");
        var progress = document.getElementById("scanProgress");
        var fill = document.getElementById("scanProgressFill");
        var text = document.getElementById("scanProgressText");
        var resultsEl = document.getElementById("scanResults");
        var logEl = document.getElementById("scanLog");
        if (!startBtn || !progress) return;

        // Start/Stop button state
        startBtn.textContent = data.running ? "Stop" : "Scan";
        startBtn.classList.toggle("danger", !!data.running);

        // Progress bar
        var pct = 0;
        var label = "Idle";
        if (data.phase === "ports") {
            pct = data.ports_total ? (data.ports_done / data.ports_total) * 100 : 0;
            label = "Scanning ports... " + data.ports_done + "/" + data.ports_total;
        } else if (data.phase === "rtsp") {
            pct = data.rtsp_total ? (data.rtsp_done / data.rtsp_total) * 100 : 0;
            label = "Probing RTSP... " + data.rtsp_done + "/" + data.rtsp_total +
                " · " + data.found + " found";
        } else if (data.phase === "done" || data.phase === "stopped") {
            pct = 100;
            label = (data.phase === "done" ? "Done" : "Stopped") +
                " — " + data.found + " camera(s) found";
        } else if (data.phase === "error") {
            label = "Error: " + (data.error || "unknown");
        }
        if (data.phase && data.phase !== "idle") progress.classList.remove("hidden");
        fill.style.width = Math.min(100, pct) + "%";
        text.textContent = label;

        // Results
        resultsEl.innerHTML = "";
        (data.results || []).forEach(function (r) {
            var row = document.createElement("div");
            row.className = "scan-result-row";

            var url = document.createElement("span");
            url.className = "scan-result-url";
            url.title = r.url + " (" + (r.detail || "") + ")";
            url.textContent = r.url;

            var copyBtn = document.createElement("button");
            copyBtn.className = "scan-result-btn";
            copyBtn.textContent = "Copy";
            copyBtn.addEventListener("click", function () {
                copyText(r.url, copyBtn);
            });

            var addBtn = document.createElement("button");
            addBtn.className = "scan-result-btn";
            if (scanAddedUrls[r.url]) {
                addBtn.textContent = "Added";
                addBtn.classList.add("added");
            } else {
                addBtn.textContent = "Add";
                addBtn.addEventListener("click", function () {
                    addScannedCamera(r.url, addBtn);
                });
            }

            row.appendChild(url);
            row.appendChild(copyBtn);
            row.appendChild(addBtn);
            resultsEl.appendChild(row);
        });

        // Log console
        if (data.log && data.log.length) {
            logEl.classList.remove("hidden");
            logEl.innerHTML = "";
            data.log.forEach(function (line) {
                var div = document.createElement("div");
                if (line.indexOf("FOUND") !== -1 || line.indexOf("RTSP URL") !== -1) {
                    div.className = "log-found";
                }
                div.textContent = line;
                logEl.appendChild(div);
            });
            logEl.scrollTop = logEl.scrollHeight;
        }
    }

    function addScannedCamera(url, btn) {
        fetch("/api/cameras", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ source: url }),
        })
            .then(function (r) {
                return r.json().then(function (data) { return { ok: r.ok, data: data }; });
            })
            .then(function (res) {
                if (res.ok) {
                    scanAddedUrls[url] = true;
                    btn.textContent = "Added";
                    btn.classList.add("added");
                    showSettingsStatus("Camera added from scan result.", "ok");
                    fetchCameras();
                } else {
                    showSettingsStatus(res.data.error || "Failed to add camera.", "err");
                }
            })
            .catch(function () {
                showSettingsStatus("Network error while adding camera.", "err");
            });
    }

    function copyText(text, btn) {
        function done() {
            var old = btn.textContent;
            btn.textContent = "Copied";
            setTimeout(function () { btn.textContent = old; }, 1200);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done, function () {
                fallbackCopy(text);
                done();
            });
        } else {
            fallbackCopy(text);
            done();
        }
    }

    function fallbackCopy(text) {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (e) {}
        document.body.removeChild(ta);
    }

    // ---- Event Bindings ----
    if (viewModeBtn) {
        viewModeBtn.addEventListener("click", toggleViewMode);
    }

    var settingsBtn = document.getElementById("settingsBtn");
    if (settingsBtn) settingsBtn.addEventListener("click", openSettings);

    if (emptyAddCamera) {
        emptyAddCamera.addEventListener("click", function () { openSettings(); });
    }

    var settingsClose = document.getElementById("settingsClose");
    if (settingsClose) settingsClose.addEventListener("click", closeSettings);

    var settingsModal = document.getElementById("settingsModal");
    if (settingsModal) {
        settingsModal.addEventListener("click", function (e) {
            if (e.target === settingsModal) closeSettings();
        });
    }

    var cameraAddBtn = document.getElementById("cameraAddBtn");
    if (cameraAddBtn) cameraAddBtn.addEventListener("click", addCamera);

    var cameraSourceInput = document.getElementById("cameraSourceInput");
    if (cameraSourceInput) {
        cameraSourceInput.addEventListener("keydown", function (e) {
            if (e.key === "Enter") addCamera();
        });
    }

    var thresholdApplyBtn = document.getElementById("thresholdApplyBtn");
    if (thresholdApplyBtn) thresholdApplyBtn.addEventListener("click", applyThresholds);

    var scanStartBtn = document.getElementById("scanStartBtn");
    if (scanStartBtn) {
        scanStartBtn.addEventListener("click", function () {
            if (scanStartBtn.textContent === "Stop") {
                stopScan();
            } else {
                startScan();
            }
        });
    }

    var scanTargetInput = document.getElementById("scanTargetInput");
    if (scanTargetInput) {
        scanTargetInput.addEventListener("keydown", function (e) {
            if (e.key === "Enter") startScan();
        });
    }

    var scanUsernameInput = document.getElementById("scanUsernameInput");
    if (scanUsernameInput) {
        scanUsernameInput.addEventListener("keydown", function (e) {
            if (e.key === "Enter") startScan();
        });
    }

    var scanPasswordInput = document.getElementById("scanPasswordInput");
    if (scanPasswordInput) {
        scanPasswordInput.addEventListener("keydown", function (e) {
            if (e.key === "Enter") startScan();
        });
    }

    // ---- Footer live clock ----
    function startClock() {
        function tick() {
            if (footClock) {
                var d = new Date();
                var p = function (n) { return String(n).padStart(2, "0"); };
                footClock.textContent = p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
            }
        }
        tick();
        setInterval(tick, 1000);
    }

    // ---- Init ----
    function init() {
        fetchCameras();
        connect();
        fetchStats();
        loadConfig();
        startClock();
    }

    init();
})();
