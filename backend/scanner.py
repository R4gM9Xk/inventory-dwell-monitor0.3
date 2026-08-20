"""
LAN RTSP Camera Scanner
=======================
Discovers RTSP cameras on the local network:

1. Expands the target specification (CIDR, IP range, or single IP).
2. Multi-threaded TCP connect scan of common RTSP ports (554, 8554, ...).
3. For every open port, probes common RTSP paths with common default
   username/password combinations using raw RTSP DESCRIBE requests
   (supports both Basic and Digest authentication).
4. Working combinations are reported as complete rtsp:// URLs and
   printed to the server console.

The scan runs in a background thread (with internal thread pools), so the
FastAPI event loop is never blocked. Progress, log lines and results are
exposed through ``RtspScanner.status()`` for the REST API.

Intended for discovering your own cameras on your own LAN.
"""

import base64
import hashlib
import ipaddress
import os
import re
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
# 100 common RTSP ports probed when the user leaves the port field blank:
#  - 554: the RTSP standard
#  - N554 pattern alternates (1554, 2554, ..., 10554, 11554, ...) used by
#    many camera / NVR vendors
#  - 5540-5543 and the 855x series (8554-8560): very common consumer-camera
#    alternates
#  - 7070/7071: RealNetworks RTSP and derivatives
#  - other ports frequently found serving RTSP in the wild
DEFAULT_PORTS = [
      554,   1054,   1554,   2054,   2554,   3054,   3554,   4054,   4554,   5054,
     5554,   6054,   6554,   7054,   7554,   8054,   8554,   9054,   9554,  10054,
    10554,  11554,  12554,  13554,  14554,  15554,  16554,  17554,  18554,  19554,
    20554,  25554,  30554,  35554,  40554,  45554,  50554,   5540,   5541,   5542,
     5543,   8555,   8556,   8557,   8558,   8559,   8560,   7070,   7071,   7777,
     7788,   5000,   5001,   5050,   5060,   6000,   6001,   6060,   7000,   7001,
     8000,   8001,   8010,   8080,   8081,   8088,   8090,   8181,   8888,   8899,
     9000,   9001,   9080,   9090,   9999,  10000,  10001,  11000,  12000,  15555,
     20000,  21000,  22000,  25000,  30000,  31000,  32000,  40000,  41000,  42000,
     45000,  50000,  51000,  52000,  55000,  55555,  60000,  61000,  62000,  65000,
]

DEFAULT_CREDENTIALS = [
    ("admin", "12345"),
    ("admin", ""),
    ("admin", "admin"),
    ("admin", "123456"),
    ("admin", "888888"),
    ("admin", "password"),
    ("admin", "1234"),
    ("root", "root"),
    ("root", "12345"),
    ("user", "user"),
]

DEFAULT_PATHS = [
    "/live.sdp",
    "/stream1",
    "/stream2",
    "/h264",
    "/h264/ch1/main/av_stream",
    "/cam/realmonitor?channel=1&subtype=0",
    "/Streaming/Channels/101",
    "/Streaming/Channels/102",
    "/11",
    "/12",
    "/onvif1",
    "/onvif2",
    "/media/video1",
    "/ch01/0",
    "/live/ch1",
    "/mpeg4",
    "/1",
    "/cam1/h264",
]

MAX_TARGETS = 4096          # safety cap on hosts per scan
MAX_PORTS = 200             # safety cap on ports per scan
MAX_LOG_LINES = 200
PORT_SCAN_WORKERS = 256
RTSP_PROBE_WORKERS = 16


# ---------------------------------------------------------------------------
# Target parsing
# ---------------------------------------------------------------------------
def parse_targets(spec: str) -> list:
    """
    Expand a target spec into a list of IP address strings.

    Accepted forms (several may be combined with commas):
      - CIDR:        192.168.1.0/24
      - Range:       192.168.1.10-192.168.1.50  or  192.168.1.10-50
      - Single IP:   192.168.1.100
      - Combined:    192.168.1.0/24,192.168.2.0/24
    """
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("Target is required (e.g. 192.168.1.0/24)")

    ips = []
    for part in spec.split(","):
        ips.extend(_expand_one(part.strip()))
    # De-duplicate while preserving order (overlapping ranges).
    seen = set()
    result = []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result


def _expand_one(spec: str) -> list:
    """Expand a single (non-comma) target spec into IP address strings."""
    if not spec:
        return []
    try:
        if "/" in spec:
            net = ipaddress.ip_network(spec, strict=False)
            hosts = [str(ip) for ip in net.hosts()]
            if not hosts:  # /31 or /32
                hosts = [str(ip) for ip in net]
            return hosts

        if "-" in spec:
            start_s, end_s = [p.strip() for p in spec.split("-", 1)]
            start = ipaddress.ip_address(start_s)
            if "." not in end_s:  # short form: 192.168.1.10-50
                prefix = start_s.rsplit(".", 1)[0]
                end_s = "{}.{}".format(prefix, end_s)
            end = ipaddress.ip_address(end_s)
            if int(end) < int(start):
                raise ValueError("Range end must be >= range start")
            return [str(ipaddress.ip_address(i))
                    for i in range(int(start), int(end) + 1)]

        ipaddress.ip_address(spec)  # validate
        return [spec]
    except ValueError as exc:
        raise ValueError("Invalid target {!r}: {}".format(spec, exc))


def get_local_networks() -> list:
    """
    Auto-detect the local LAN(s) this machine is attached to.

    Finds the machine's own IPv4 addresses (via a UDP connect probe, which
    sends no packets, plus hostname resolution), filters out loopback and
    link-local addresses, and returns each distinct /24 network.
    """
    ips = set()

    # UDP "connect" picks the right source interface without sending traffic.
    for dest in ("8.8.8.8", "114.114.114.114", "223.5.5.5",
                 "192.168.1.1", "192.168.0.1", "10.0.0.1", "172.16.0.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.5)
            sock.connect((dest, 80))
            ips.add(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            sock.close()

    # Hostname resolution catches additional interfaces (e.g. VPNs).
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        ips.update(addrs)
    except OSError:
        pass

    networks = []
    for ip in sorted(ips):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_multicast \
                or not addr.is_private:
            continue
        net = ipaddress.ip_network("{}/24".format(ip), strict=False)
        if net not in networks:
            networks.append(net)
    return networks


def parse_ports(value) -> list:
    """Normalize a ports argument (list or comma string) into a validated list."""
    if value is None:
        return list(DEFAULT_PORTS)
    if isinstance(value, str):
        raw = [p.strip() for p in value.split(",") if p.strip()]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raise ValueError("ports must be a list or comma-separated string")

    ports = []
    for p in raw:
        port = int(p)
        if not 1 <= port <= 65535:
            raise ValueError("Invalid port: {}".format(p))
        if port not in ports:
            ports.append(port)
    if not ports:
        raise ValueError("At least one port is required")
    return ports[:MAX_PORTS]


# ---------------------------------------------------------------------------
# Low-level RTSP probing
# ---------------------------------------------------------------------------
def _check_port(ip: str, port: int, timeout: float) -> bool:
    """Return True if a TCP connection to ip:port succeeds."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _rtsp_request(ip, port, path, auth_header=None, timeout=2.0):
    """
    Send one RTSP DESCRIBE request and return (status_code, headers).
    status_code is None when the peer did not speak RTSP.
    """
    url = "rtsp://{}:{}{}".format(ip, port, path)
    lines = [
        "DESCRIBE {} RTSP/1.0".format(url),
        "CSeq: 1",
        "User-Agent: dwell-monitor-rtsp-scanner",
        "Accept: application/sdp",
    ]
    if auth_header:
        lines.append("Authorization: {}".format(auth_header))
    payload = "\r\n".join(lines) + "\r\n\r\n"

    with socket.create_connection((ip, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload.encode("latin-1"))
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 8192:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk

    text = data.decode("latin-1", errors="replace")
    if not text.startswith("RTSP/1."):
        return None, {}
    status_line, _, rest = text.partition("\r\n")
    parts = status_line.split()
    code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    headers = {}
    for line in rest.split("\r\n\r\n", 1)[0].split("\r\n"):
        key, _, val = line.partition(":")
        if key:
            headers[key.strip().lower()] = val.strip()
    return code, headers


def _build_auth_header(authenticate, url, username, password):
    """Build an Authorization header value from a WWW-Authenticate challenge."""
    scheme = (authenticate or "").strip().lower()
    if not scheme or scheme.startswith("basic"):
        token = base64.b64encode(
            "{}:{}".format(username, password).encode("latin-1")
        ).decode("ascii")
        return "Basic {}".format(token)

    if scheme.startswith("digest"):
        params = {}
        for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', authenticate):
            params[match.group(1).lower()] = match.group(2) or match.group(3) or ""
        realm = params.get("realm", "")
        nonce = params.get("nonce", "")
        ha1 = hashlib.md5(
            "{}:{}:{}".format(username, realm, password).encode("latin-1")
        ).hexdigest()
        ha2 = hashlib.md5("DESCRIBE:{}".format(url).encode("latin-1")).hexdigest()
        qop = params.get("qop", "")
        base = 'Digest username="{}", realm="{}", nonce="{}", uri="{}"'.format(
            username, realm, nonce, url)
        if qop:
            qop = qop.split(",")[0].strip() or "auth"
            nc = "00000001"
            cnonce = hashlib.md5(os.urandom(8)).hexdigest()[:16]
            response = hashlib.md5(
                "{}:{}:{}:{}:{}:{}".format(ha1, nonce, nc, cnonce, qop, ha2)
                .encode("latin-1")
            ).hexdigest()
            return '{}, response="{}", qop={}, nc={}, cnonce="{}"'.format(
                base, response, qop, nc, cnonce)
        response = hashlib.md5(
            "{}:{}:{}".format(ha1, nonce, ha2).encode("latin-1")
        ).hexdigest()
        return '{}, response="{}"'.format(base, response)

    return None


def _make_url(ip, port, path, username=None, password=None):
    if username is None:
        return "rtsp://{}:{}{}".format(ip, port, path)
    return "rtsp://{}:{}@{}:{}{}".format(username, password or "", ip, port, path)


def probe_endpoint(ip, port, credentials, paths, timeout, stop_event):
    """
    Probe one ip:port for a working RTSP stream.

    Returns (url, detail) on success, (None, reason) otherwise.
    """
    # First request without credentials on the first candidate path.
    try:
        code, headers = _rtsp_request(ip, port, paths[0], timeout=timeout)
    except OSError:
        return None, "no RTSP response"

    if code is None:
        return None, "not an RTSP service"
    if code == 200:
        return _make_url(ip, port, paths[0]), "no authentication required"

    authenticate = headers.get("www-authenticate", "")

    if code != 401:
        # Possibly just a wrong path — walk paths unauthenticated.
        for path in paths[1:]:
            if stop_event.is_set():
                return None, "stopped"
            try:
                code, headers = _rtsp_request(ip, port, path, timeout=timeout)
            except OSError:
                continue
            if code == 200:
                return _make_url(ip, port, path), "no authentication required"
            if code == 401:
                authenticate = headers.get("www-authenticate", "")
                break
        else:
            return None, "no known path answered (HTTP {})".format(code)
        if code != 401:
            return None, "no known path answered (HTTP {})".format(code)

    # Credentials required — try default username/password combinations.
    valid_cred = None
    for username, password in credentials:
        if stop_event.is_set():
            return None, "stopped"
        auth = _build_auth_header(
            authenticate, "rtsp://{}:{}{}".format(ip, port, paths[0]),
            username, password)
        try:
            code, _ = _rtsp_request(ip, port, paths[0],
                                    auth_header=auth, timeout=timeout)
        except OSError:
            continue
        if code == 200:
            return _make_url(ip, port, paths[0], username, password), \
                "credentials {} / {}".format(username, password or "(empty)")
        if code is not None and code not in (401, 403):
            valid_cred = (username, password)  # auth accepted, wrong path
            break

    if valid_cred is None:
        return None, "no default credentials worked"

    # Credentials valid but first path wrong — find a working path.
    username, password = valid_cred
    for path in paths[1:]:
        if stop_event.is_set():
            return None, "stopped"
        auth = _build_auth_header(
            authenticate, "rtsp://{}:{}{}".format(ip, port, path),
            username, password)
        try:
            code, _ = _rtsp_request(ip, port, path,
                                    auth_header=auth, timeout=timeout)
        except OSError:
            continue
        if code == 200:
            return _make_url(ip, port, path, username, password), \
                "credentials {} / {}".format(username, password or "(empty)")

    return None, "credentials accepted but no known path worked"


# ---------------------------------------------------------------------------
# Scanner (background thread, thread-safe state)
# ---------------------------------------------------------------------------
class RtspScanner:
    """Manages one network scan at a time and exposes progress state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._reset_state()

    # ---- state ----

    def _reset_state(self):
        self.running = False
        self.phase = "idle"          # idle | ports | rtsp | done | stopped | error
        self.targets = 0
        self.ports_total = 0
        self.ports_done = 0
        self.rtsp_total = 0
        self.rtsp_done = 0
        self.results = []            # [{ip, port, url, detail}]
        self.logs = deque(maxlen=MAX_LOG_LINES)
        self.started_at = None
        self.error = None

    # ---- control ----

    def start(self, target, ports=None, credentials=None, paths=None,
              timeout=1.0):
        """Start a scan. Returns (ok, message)."""
        with self._lock:
            if self.running:
                return False, "A scan is already running"

        # Blank target -> auto-detect the LAN(s) this machine is attached to.
        spec = (target or "").strip()
        auto_note = None
        if not spec:
            networks = get_local_networks()
            if not networks:
                return False, (
                    "Could not auto-detect the local network — "
                    "please enter a target manually (e.g. 192.168.1.0/24)")
            spec = ",".join(str(n) for n in networks)
            auto_note = "auto-detected LAN(s): {}".format(spec)

        try:
            ips = parse_targets(spec)
            ports = parse_ports(ports)
        except (ValueError, TypeError) as exc:
            return False, str(exc)

        if len(ips) > MAX_TARGETS:
            return False, "Too many hosts ({}) — maximum is {}".format(
                len(ips), MAX_TARGETS)

        credentials = credentials or DEFAULT_CREDENTIALS
        paths = paths or DEFAULT_PATHS

        with self._lock:
            self._reset_state()
            self.running = True
            self.started_at = time.time()
            self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run,
            args=(ips, ports, credentials, paths, timeout),
            daemon=True,
        )
        self._thread.start()
        message = "Scan started: {} host(s) x {} port(s)".format(
            len(ips), len(ports))
        if auto_note:
            message = "{} ({})".format(message, auto_note)
        return True, message

    def stop(self):
        self._stop_event.set()
        return True

    def status(self):
        with self._lock:
            return {
                "running": self.running,
                "phase": self.phase,
                "targets": self.targets,
                "ports_done": self.ports_done,
                "ports_total": self.ports_total,
                "rtsp_done": self.rtsp_done,
                "rtsp_total": self.rtsp_total,
                "found": len(self.results),
                "results": list(self.results),
                "log": list(self.logs),
                "started_at": self.started_at,
                "error": self.error,
            }

    # ---- internals ----

    def _log(self, msg):
        line = "[{}] {}".format(time.strftime("%H:%M:%S"), msg)
        print("[SCAN] {}".format(msg), flush=True)   # server console output
        with self._lock:
            self.logs.append(line)

    def _run(self, ips, ports, credentials, paths, timeout):
        rtsp_timeout = max(2.0, timeout * 2)
        try:
            # ---- Phase 1: TCP port scan (multi-threaded) ----
            with self._lock:
                self.phase = "ports"
                self.targets = len(ips)
                self.ports_total = len(ips) * len(ports)
            self._log("Port scan started: {} host(s) x {} port(s) {}".format(
                len(ips), len(ports), ports))

            open_endpoints = []
            with ThreadPoolExecutor(max_workers=PORT_SCAN_WORKERS) as pool:
                futures = {
                    pool.submit(_check_port, ip, port, timeout): (ip, port)
                    for ip in ips for port in ports
                }
                for fut in as_completed(futures):
                    if self._stop_event.is_set():
                        break
                    ip, port = futures[fut]
                    try:
                        if fut.result():
                            open_endpoints.append((ip, port))
                            self._log("{}:{} is OPEN".format(ip, port))
                    except Exception:
                        pass
                    with self._lock:
                        self.ports_done += 1

            with self._lock:
                self.ports_done = self.ports_total

            if self._stop_event.is_set():
                with self._lock:
                    self.phase = "stopped"
                self._log("Scan stopped by user.")
                return

            # ---- Phase 2: RTSP probing on open endpoints ----
            with self._lock:
                self.phase = "rtsp"
                self.rtsp_total = len(open_endpoints)
            self._log("Port scan finished: {} open endpoint(s). "
                      "Probing RTSP paths/credentials...".format(
                          len(open_endpoints)))

            if open_endpoints:
                with ThreadPoolExecutor(max_workers=RTSP_PROBE_WORKERS) as pool:
                    futures = {
                        pool.submit(probe_endpoint, ip, port, credentials,
                                    paths, rtsp_timeout,
                                    self._stop_event): (ip, port)
                        for ip, port in open_endpoints
                    }
                    for fut in as_completed(futures):
                        if self._stop_event.is_set():
                            break
                        ip, port = futures[fut]
                        try:
                            url, detail = fut.result()
                        except Exception as exc:
                            url, detail = None, "error: {}".format(exc)
                        if url:
                            with self._lock:
                                self.results.append({
                                    "ip": ip,
                                    "port": port,
                                    "url": url,
                                    "detail": detail,
                                })
                            self._log("FOUND camera -> {} ({})".format(url, detail))
                        else:
                            self._log("{}:{} - {}".format(ip, port, detail))
                        with self._lock:
                            self.rtsp_done += 1

            with self._lock:
                self.rtsp_done = self.rtsp_total
                self.phase = "stopped" if self._stop_event.is_set() else "done"

            with self._lock:
                found = len(self.results)
            if found:
                self._log("Scan finished: {} camera(s) found.".format(found))
                for r in self.results:
                    self._log("  RTSP URL: {}".format(r["url"]))
            else:
                self._log("Scan finished: no cameras found.")

        except Exception as exc:  # never kill the server thread silently
            with self._lock:
                self.phase = "error"
                self.error = str(exc)
            self._log("Scan error: {}".format(exc))
        finally:
            with self._lock:
                self.running = False
