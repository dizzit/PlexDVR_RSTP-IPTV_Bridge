#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Plex Camera IPTV Gateway — v7.7.1b (compile‑ready)

This build keeps the stable v7.7b streaming core (copy H.264 + AUD insert)
+ adds back the two UI features AND a simple Mosaic channel:
  • Right‑click context menu on rows (Edit…, Remove, Check Status,
    Edit Channel Number…, Edit Mosaic… if applicable)
  • Drag‑and‑drop row reordering with correct channel‑number shifting
  • Add Mosaic button (up to 4 sources). Mosaic encodes video (libx264,
    low‑latency) and uses audio from the first source.

Notes
  • Non‑mosaic channels: H.264 copy + h264_mp4toannexb + AUD insert (as before).
  • Mosaic channels: a small xstack layout; no 'fifo' filter (better
    compatibility with some builds).
  • Optional XMLTV & M3U: /xmltv  /m3u
  • Place ffmpeg/ffprobe next to the EXE (or on PATH).

Build (PowerShell one‑liner):
  pyinstaller --clean --noconfirm --name PlexCamGateway ^
    --collect-all PyQt6 ^
    --add-binary "ffmpeg.exe;." ^
    --add-binary "ffprobe.exe;." ^
    plex_cam_gateway_gui_v7_7_1b_compile_ready.py
"""
from __future__ import annotations

import os, sys, io, json, shutil, socket, signal, threading, subprocess, base64, re, time
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, urlunparse, quote
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request, make_response
from werkzeug.serving import make_server

from PyQt6.QtCore import Qt, QThreadPool, QRunnable, pyqtSlot, QObject, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QInputDialog, QMessageBox, QFileDialog, QSpinBox, QHeaderView, QMenu, QAbstractItemView
)

try:
    import yaml
except Exception:
    yaml = None

# ======================== Utilities & ffmpeg discovery ========================

def _exe_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# prepend our dir to PATH so bundled ffmpeg/ffprobe are found
os.environ["PATH"] = _exe_dir() + os.pathsep + os.environ.get("PATH", "")

FFMPEG = shutil.which("ffmpeg") or os.path.join(_exe_dir(), "ffmpeg.exe")
FFPROBE = shutil.which("ffprobe") or os.path.join(_exe_dir(), "ffprobe.exe")

UA = "LibVLC/3.0.20 (LIVE555 Streaming Media v2021.12.30)"
TRANSPORTS = ("Auto", "TCP", "UDP")
AUTH_MODES = ("Auto", "Header-Basic")
DEFAULT_TIMEOUT = 12

STATE_LOCK = threading.RLock()

# ================================ Channel Model ===============================
class Channel:
    def __init__(self, ch_id: str, name: str, rtsp: str,
                 transport: str = "Auto",
                 username: str = "", password: str = "",
                 auth_mode: str = "Auto",
                 transcode_audio: bool = True,
                 tvg_id: Optional[str] = None,
                 tvg_logo: str = "",
                 epg_title: Optional[str] = None,
                 epg_desc: str = "Live feed",
                 mosaic_sources: Optional[List[str]] = None):
        self.id = str(ch_id)
        self.name = name or f"Channel {ch_id}"
        self.rtsp = rtsp
        self.transport = transport if transport in TRANSPORTS else "Auto"
        self.username = username
        self.password = password
        self.auth_mode = auth_mode if auth_mode in AUTH_MODES else "Auto"
        self.transcode_audio = bool(transcode_audio)
        self.tvg_id = tvg_id or f"cam.{ch_id}"
        self.tvg_logo = tvg_logo
        self.epg_title = epg_title or self.name
        self.epg_desc = epg_desc
        self.mosaic_sources = list(mosaic_sources) if mosaic_sources else None
        # GUI only
        self.status = "Idle"
        self.status_detail = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "rtsp": self.rtsp,
            "transport": self.transport,
            "username": self.username,
            "password": self.password,
            "auth_mode": self.auth_mode,
            "transcode_audio": self.transcode_audio,
            "tvg_id": self.tvg_id,
            "tvg_logo": self.tvg_logo,
            "epg_title": self.epg_title,
            "epg_desc": self.epg_desc,
            "mosaic_sources": self.mosaic_sources or [],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'Channel':
        return Channel(
            ch_id=str(d.get("id")),
            name=d.get("name", f"Channel {d.get('id')}") or f"Channel {d.get('id')}",
            rtsp=d.get("rtsp", ""),
            transport=d.get("transport", "Auto"),
            username=d.get("username", ""),
            password=d.get("password", ""),
            auth_mode=d.get("auth_mode", "Auto"),
            transcode_audio=bool(d.get("transcode_audio", True)),
            tvg_id=d.get("tvg_id") or None,
            tvg_logo=d.get("tvg_logo", ""),
            epg_title=d.get("epg_title") or None,
            epg_desc=d.get("epg_desc", "Live feed"),
            mosaic_sources=d.get("mosaic_sources") or None,
        )

    # RTSP auth injection (only when scheme is rtsp and user didn’t embed creds)
    def auth_url(self) -> str:
        try:
            p = urlparse(self.rtsp)
            if p.scheme.lower().startswith("rtsp"):
                if p.username or p.password or (not self.username and not self.password):
                    return self.rtsp
                user_enc = quote(self.username or "", safe="")
                pass_enc = quote(self.password or "", safe="")
                netloc = p.netloc
                if ":" in netloc:
                    host, port = netloc.split(":", 1)
                    netloc = f"{user_enc}:{pass_enc}@{host}:{port}"
                else:
                    netloc = f"{user_enc}:{pass_enc}@{netloc}"
                return urlunparse((p.scheme, netloc, p.path or "", p.params or "", p.query or "", p.fragment or ""))
            return self.rtsp
        except Exception:
            return self.rtsp

    def basic_auth_header(self) -> Optional[str]:
        if self.auth_mode != "Header-Basic":
            return None
        token = f"{self.username}:{self.password}".encode("utf-8")
        return "Authorization: Basic " + base64.b64encode(token).decode("ascii")

    def merged_headers(self) -> str:
        h = []
        bh = self.basic_auth_header()
        if bh:
            h.append(bh)
        return "\r\n".join(h) if h else ""

# Global state
CHANNELS: List[Channel] = []
SERVER_CFG = {"name": "CamIPTV", "host": "0.0.0.0", "port": 8000, "device_id": None}

# ================================ Flask App ==================================
app = Flask(__name__)

# ---- helpers ----

def _device_id() -> str:
    if not SERVER_CFG.get("device_id"):
        import uuid
        h = (uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname()).int) & 0xFFFFFFFF
        SERVER_CFG["device_id"] = f"{h:08X}"
    return SERVER_CFG["device_id"]


def _ffmpeg_input_for_channel(ch: Channel) -> List[str]:
    parts: List[str] = []
    # transport flags for RTSP
    if ch.transport == "TCP" and ch.rtsp.lower().startswith("rtsp://"):
        parts += ["-rtsp_transport", "tcp", "-rtsp_flags", "prefer_tcp"]
    elif ch.transport == "UDP" and ch.rtsp.lower().startswith("rtsp://"):
        parts += ["-rtsp_transport", "udp"]
    # low-ish latency and quicker lock
    parts += [
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-analyzeduration", "100000",
        "-probesize", "32768",
        "-user_agent", UA,
    ]
    hdr = ch.merged_headers()
    if hdr:
        parts += ["-headers", hdr]
    parts += ["-i", ch.auth_url()]
    return parts


def _mosaic_filter_and_layout(n_inputs: int) -> (str, str):
    # Each tile 640x360 in a 1280x720 canvas. Layouts for 2–4 inputs.
    # return (filter_complex, vout_label)
    tile_w, tile_h = 640, 360
    chains = []
    labels = []
    for i in range(n_inputs):
        chains.append(f"[{i}:v]scale={tile_w}:{tile_h}:force_original_aspect_ratio=decrease,pad={tile_w}:{tile_h}:( {tile_w}-iw)/2:( {tile_h}-ih)/2:black,setsar=1[v{i}]")
        labels.append(f"[v{i}]")
    if n_inputs == 2:
        layout = "0_0|640_0"
    elif n_inputs == 3:
        layout = "0_0|640_0|0_360"
    else:  # 4 or more -> take first 4
        layout = "0_0|640_0|0_360|640_360"
    x_n = min(n_inputs, 4)
    chains.append(f"{''.join(labels[:x_n])}xstack=inputs={x_n}:layout={layout}:fill=black[vout]")
    return ";".join(chains), "[vout]"


def ffmpeg_cmd_for_channel(ch: Channel) -> List[str]:
    # Mosaic path requires multiple inputs and re-encode video
    if ch.mosaic_sources and len(ch.mosaic_sources) >= 2:
        # Build a safe list of source Channels by ID
        with STATE_LOCK:
            id_map = {c.id: c for c in CHANNELS}
            srcs: List[Channel] = [id_map[i] for i in ch.mosaic_sources if i in id_map]
        srcs = srcs[:4]
        if len(srcs) < 2:
            # Fallback to first real URL if something went wrong
            return ffmpeg_cmd_for_channel(Channel(ch.id, ch.name, ch.rtsp, ch.transport, ch.username, ch.password, ch.auth_mode, ch.transcode_audio))
        cmd: List[str] = [FFMPEG, "-nostats", "-loglevel", "error"]
        # per-input flags then -i
        for s in srcs:
            cmd += _ffmpeg_input_for_channel(s)  # includes -i
        # filter_complex without fifo for broader compatibility
        fc, vout = _mosaic_filter_and_layout(len(srcs))
        cmd += ["-filter_complex", fc, "-map", vout]
        # pick audio from first input
        cmd += ["-map", "0:a:0?"]
        # encode video for mosaic (low latency)
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-g", "60", "-keyint_min", "60",
            "-pix_fmt", "yuv420p",
        ]
        # audio transcode or copy
        if ch.transcode_audio:
            cmd += ["-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k"]
        else:
            cmd += ["-c:a", "copy"]
        cmd += [
            "-max_muxing_queue_size", "1024",
            "-flush_packets", "1",
            "-muxpreload", "0", "-muxdelay", "0",
            "-mpegts_flags", "+initial_discontinuity+resend_headers",
            "-pat_period", "0.2",
            "-metadata", f"service_provider={SERVER_CFG['name']}",
            "-metadata", f"service_name={ch.name}",
            "-f", "mpegts", "pipe:1",
        ]
        return cmd

    # Non‑mosaic (single source): proven copy path
    cmd: List[str] = [FFMPEG, "-nostats", "-loglevel", "error"]
    cmd += _ffmpeg_input_for_channel(ch)
    cmd += [
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "copy",
        "-bsf:v", "h264_mp4toannexb,h264_metadata=aud=insert",
    ]
    if ch.transcode_audio:
        cmd += ["-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k"]
    else:
        cmd += ["-c:a", "copy"]
    cmd += [
        "-flush_packets", "1",
        "-muxpreload", "0", "-muxdelay", "0",
        "-mpegts_flags", "+initial_discontinuity+resend_headers",
        "-pat_period", "0.2",
        "-metadata", f"service_provider={SERVER_CFG['name']}",
        "-metadata", f"service_name={ch.name}",
        "-f", "mpegts", "pipe:1",
    ]
    return cmd


def _stderr_logger(proc: subprocess.Popen, ch_id: str):
    try:
        for line in iter(proc.stderr.readline, b""):
            if not line:
                break
            try:
                print(f"[FFMPEG ch {ch_id}] {line.decode(errors='ignore').rstrip()}")
            except Exception:
                pass
    except Exception:
        pass


def stream_generator(ch: Channel):
    backoff = 1.0
    while True:
        proc = subprocess.Popen(ffmpeg_cmd_for_channel(ch), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        t = threading.Thread(target=_stderr_logger, args=(proc, ch.id), daemon=True)
        t.start()
        any_bytes = False
        try:
            while True:
                chunk = proc.stdout.read(1316)
                if not chunk:
                    break
                any_bytes = True
                yield chunk
        finally:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
        backoff = 1.0 if any_bytes else min(backoff * 1.7, 10.0)
        print(f"[TUNE] stream for {ch.id} ended; restarting in {backoff:.1f}s\n")
        time.sleep(backoff)


# ---- endpoints used by Plex ----
@app.route("/discover.json")
def discover_json():
    base = request.host_url.rstrip("/")
    return jsonify({
        "FriendlyName": f"{SERVER_CFG['name']} (IPTV Bridge)",
        "ModelNumber": "HDHR4-2US",
        "FirmwareName": "hdhomerun_ip",
        "FirmwareVersion": "2024.06.01",
        "DeviceID": _device_id(),
        "DeviceAuth": "plexcam",
        "BaseURL": base,
        "LineupURL": f"{base}/lineup.json",
        "LineupStatus": {"ScanInProgress": 0, "ScanPossible": 1, "Source": "Cable", "SourceList": ["Cable"]},
        "TunerCount": 1
    })


@app.route("/lineup_status.json")
def lineup_status():
    return jsonify({"ScanInProgress": 0, "ScanPossible": 1, "Source": "Cable", "SourceList": ["Cable"]})


@app.route("/lineup.json")
def lineup_json():
    base = request.host_url.rstrip("/")
    with STATE_LOCK:
        chs = list(CHANNELS)
    rows = []
    for c in chs:
        rows.append({
            "GuideNumber": str(c.id),
            "GuideName": c.name,
            "URL": f"{base}/auto/v{c.id}",
        })
    return jsonify(rows)


def _no_cache_head_response():
    r = make_response(b"")
    r.mimetype = "video/mp2t"
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r


def _stream_response(gen):
    resp = Response(gen, mimetype="video/mp2t", direct_passthrough=True)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/auto/v<int:ch_num>", methods=["GET", "HEAD"])
@app.route("/auto/v<int:ch_num>.ts", methods=["GET", "HEAD"])
def auto_v(ch_num: int):
    ch_id = str(ch_num)
    with STATE_LOCK:
        ch = next((c for c in CHANNELS if c.id == ch_id), None)
    if not ch:
        return jsonify({"error": "channel not found"}), 404
    if request.method == "HEAD":
        print(f"[TUNE] HEAD /auto/v{ch_id} from {request.remote_addr}")
        return _no_cache_head_response()
    print(f"[TUNE] /auto/v{ch_id} requested from {request.remote_addr}")
    return _stream_response(stream_generator(ch))


# ---- XMLTV & M3U ----
@app.route("/xmltv")
def xmltv():
    try:
        hours = int(request.args.get("hours", "24"))
    except Exception:
        hours = 24
    hours = max(1, min(hours, 168))
    try:
        slot = int(request.args.get("slot", "30"))
    except Exception:
        slot = 30
    slot = max(5, min(slot, 240))

    with STATE_LOCK:
        chs = list(CHANNELS)

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=hours)

    s = io.StringIO()
    s.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    s.write('<tv generator-info-name="PlexCam v7.7.1b">\n')
    for c in chs:
        s.write(f'  <channel id="{c.tvg_id}">\n')
        s.write(f'    <display-name>{c.name}</display-name>\n')
        if c.tvg_logo:
            s.write(f'    <icon src="{c.tvg_logo}"/>\n')
        s.write('  </channel>\n')
    for c in chs:
        t = now
        while t < end:
            start = t.strftime("%Y%m%d%H%M%S +0000")
            stop = (t + timedelta(minutes=slot)).strftime("%Y%m%d%H%M%S +0000")
            title = c.epg_title or c.name
            desc = c.epg_desc or ("Mosaic feed" if c.mosaic_sources else "Live feed")
            s.write(f'  <programme start="{start}" stop="{stop}" channel="{c.tvg_id}">\n')
            s.write(f'    <title lang="en">{title}</title>\n')
            s.write(f'    <desc lang="en">{desc}</desc>\n')
            s.write('  </programme>\n')
            t += timedelta(minutes=slot)
    s.write('</tv>\n')
    return Response(s.getvalue(), mimetype="application/xml")


@app.route("/m3u")
def m3u():
    base = request.host_url.rstrip("/")
    with STATE_LOCK:
        chs = list(CHANNELS)
    lines = [f'#EXTM3U x-tvg-url="{base}/xmltv"']
    for c in chs:
        lines.append(f'#EXTINF:-1 tvg-id="{c.tvg_id}" tvg-logo="{c.tvg_logo}",{c.name}')
        lines.append(f"{base}/auto/v{c.id}")
    return Response("\n".join(lines) + "\n", mimetype="application/x-mpegURL")


# ============================== HTTP Server ctl ===============================
_HTTPD = None
_SERVER_THREAD: Optional[threading.Thread] = None
_SERVER_RUNNING = False

def start_http_server() -> bool:
    global _HTTPD, _SERVER_THREAD, _SERVER_RUNNING
    if _SERVER_RUNNING:
        return True
    try:
        _HTTPD = make_server(SERVER_CFG["host"], SERVER_CFG["port"], app)
    except OSError as e:
        print(f"Failed to start server: {e}")
        return False
    def _serve():
        global _SERVER_RUNNING
        _SERVER_RUNNING = True
        try:
            _HTTPD.serve_forever()
        finally:
            _SERVER_RUNNING = False
    _SERVER_THREAD = threading.Thread(target=_serve, daemon=True)
    _SERVER_THREAD.start()
    return True

def stop_http_server():
    global _HTTPD, _SERVER_THREAD, _SERVER_RUNNING
    if not _SERVER_RUNNING or _HTTPD is None:
        return
    try:
        _HTTPD.shutdown()
    except Exception:
        pass
    if _SERVER_THREAD:
        _SERVER_THREAD.join(timeout=2.0)
    _SERVER_RUNNING = False
    _HTTPD = None
    _SERVER_THREAD = None


# ============================= Probing (ffprobe) ==============================
class ProbeSignals(QObject):
    result = pyqtSignal(int, str, str)

class ProbeWorker(QRunnable):
    def __init__(self, row: int, ch: Channel):
        super().__init__()
        self.row = row
        self.ch = ch
        self.signals = ProbeSignals()

    @pyqtSlot()
    def run(self):
        # For mosaic, probe the first valid source
        target_url = None
        if self.ch.mosaic_sources:
            with STATE_LOCK:
                id_map = {c.id: c for c in CHANNELS}
                for cid in self.ch.mosaic_sources:
                    c = id_map.get(cid)
                    if c:
                        target_url = c.auth_url()
                        break
        url = target_url or self.ch.auth_url()
        if not (shutil.which(os.path.basename(FFPROBE)) or os.path.exists(FFPROBE)):
            self.signals.result.emit(self.row, "Error", "ffprobe not found")
            return
        tries: List[str] = ["tcp", "udp"] if url.lower().startswith("rtsp://") else ["http"]
        hdr = self.ch.merged_headers() or None
        last_err = "Unknown"
        for tr in tries:
            cmd = [FFPROBE, "-v", "error", "-user_agent", UA]
            if tr in ("tcp", "udp"):
                cmd += ["-rtsp_transport", tr]
                if tr == "tcp":
                    cmd += ["-rtsp_flags", "prefer_tcp"]
            if hdr:
                cmd += ["-headers", hdr]
            cmd += ["-i", url, "-show_streams", "-select_streams", "v:0"]
            try:
                p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=DEFAULT_TIMEOUT)
                if p.returncode == 0:
                    mode = "mosaic" if self.ch.mosaic_sources else ("rtsp" if url.startswith("rtsp") else "http")
                    self.signals.result.emit(self.row, "OK", f"probe {mode} ok")
                    return
                else:
                    last_err = p.stderr.decode(errors="ignore") or f"probe {tr} failed"
            except subprocess.TimeoutExpired:
                last_err = f"probe {tr} timeout"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
        self.signals.result.emit(self.row, "Error", last_err[:400])


# ================================== GUI ======================================
class ChannelTable(QTableWidget):
    def __init__(self, parent):
        super().__init__(0, 9, parent)
        self._drag_row = None
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)  # custom reorder
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropOverwriteMode(False)

    def mousePressEvent(self, e):
        self._drag_row = self.rowAt(e.pos().y())
        super().mousePressEvent(e)

    def dropEvent(self, e):
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        dst = self.rowAt(pos.y())
        if dst < 0:
            dst = self.rowCount() - 1
        src = self._drag_row if self._drag_row is not None else self.currentRow()
        self._drag_row = None
        if src < 0 or dst < 0 or src == dst:
            e.ignore(); return
        self.parent().reorder_rows(src, dst)
        e.acceptProposedAction()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Plex Camera IPTV Gateway (v7.7.1b)")
        self.resize(1450, 760)
        self.threadpool = QThreadPool.globalInstance()

        layout = QVBoxLayout(self)

        # top bar
        top = QHBoxLayout()
        self.btn_add = QPushButton("Add")
        self.btn_add_mosaic = QPushButton("Add Mosaic")
        self.btn_remove = QPushButton("Remove")
        self.btn_check = QPushButton("Check Status")
        self.btn_toggle_audio = QPushButton("Toggle Audio (AAC/Copy)")
        self.btn_copy = QPushButton("Copy Stream URL")
        self.btn_start = QPushButton("Start Server")
        self.btn_stop = QPushButton("Stop Server")
        self.btn_save = QPushButton("Save Config")
        self.btn_load = QPushButton("Load Config")
        self.port_label = QLabel("Port:")
        self.port_spin = QSpinBox(); self.port_spin.setRange(1024, 65535); self.port_spin.setValue(SERVER_CFG["port"])
        self.ff_label = QLabel(f"ffmpeg: {'OK' if shutil.which('ffmpeg') or os.path.exists(FFMPEG) else 'MISSING'} | "
                               f"ffprobe: {'OK' if shutil.which('ffprobe') or os.path.exists(FFPROBE) else 'MISSING'}")
        for w in (self.btn_add, self.btn_add_mosaic, self.btn_remove, self.btn_check, self.btn_toggle_audio, self.btn_copy,
                  self.btn_start, self.btn_stop, self.btn_save, self.btn_load, self.port_label, self.port_spin, self.ff_label):
            top.addWidget(w)
        layout.addLayout(top)

        # table
        self.table = ChannelTable(self)
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels(["Channel", "RTSP/HLS/Mosaic", "Transport", "Auth Mode", "Username", "Password", "Audio", "Type", "Status"])
        modes = (QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.Stretch,
                 QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.ResizeToContents,
                 QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.ResizeToContents,
                 QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.ResizeToContents,
                 QHeaderView.ResizeMode.ResizeToContents)
        for i, m in enumerate(modes):
            self.table.horizontalHeader().setSectionResizeMode(i, m)
        layout.addWidget(self.table)

        # context menu
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_context_menu)

        # info
        p = self.port_spin.value()
        info = (f"Add to Plex (manual HDHR): http://<your-ip>:{p}\n"
                f"M3U:  http://<your-ip>:{p}/m3u\n"
                f"XMLTV: http://<your-ip>:{p}/xmltv\n"
                f"Direct test: http://<your-ip>:{p}/auto/v<channel>")
        self.info = QLabel(info)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.info)

        # connections
        self.btn_add.clicked.connect(self.on_add)
        self.btn_add_mosaic.clicked.connect(self.on_add_mosaic)
        self.btn_remove.clicked.connect(self.on_remove)
        self.btn_check.clicked.connect(self.on_check_status)
        self.btn_toggle_audio.clicked.connect(self.on_toggle_audio)
        self.btn_copy.clicked.connect(self.on_copy_url)
        self.btn_start.clicked.connect(self.on_start_server)
        self.btn_stop.clicked.connect(self.on_stop_server)
        self.btn_save.clicked.connect(self.on_save_config)
        self.btn_load.clicked.connect(self.on_load_config)
        self.port_spin.valueChanged.connect(self.on_port_changed)

        # auto-load config.yaml if present
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        if os.path.exists(cfg_path):
            try:
                self.load_from_file(cfg_path)
            except Exception as e:
                QMessageBox.warning(self, "Load", f"Failed to load config.yaml: {e}")

    # ---------- table helpers ----------
    def _refresh_table(self):
        with STATE_LOCK:
            chs = list(CHANNELS)
        self.table.setRowCount(len(chs))
        for row, ch in enumerate(chs):
            audio = "AAC" if ch.transcode_audio else "Copy"
            src_text = (
                f"mosaic://{','.join(ch.mosaic_sources)}" if ch.mosaic_sources else ch.rtsp
            )
            typ = "Mosaic" if ch.mosaic_sources else ("RTSP/HLS")
            self.table.setItem(row, 0, QTableWidgetItem(str(ch.id)))
            self.table.setItem(row, 1, QTableWidgetItem(src_text))
            self.table.setItem(row, 2, QTableWidgetItem(ch.transport))
            self.table.setItem(row, 3, QTableWidgetItem(ch.auth_mode))
            self.table.setItem(row, 4, QTableWidgetItem(ch.username))
            self.table.setItem(row, 5, QTableWidgetItem("********" if ch.password else ""))
            self.table.setItem(row, 6, QTableWidgetItem(audio))
            self.table.setItem(row, 7, QTableWidgetItem(typ))
            s_item = QTableWidgetItem(ch.status)
            s_item.setToolTip(ch.status_detail or "")
            self.table.setItem(row, 8, s_item)

    def _selected_rows(self) -> List[int]:
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def _next_channel_id(self) -> str:
        with STATE_LOCK:
            nums = [int(c.id) for c in CHANNELS if str(c.id).isdigit()]
        return str(max(nums) + 1 if nums else 101)

    def _validate_source(self, url: str) -> bool:
        u = (url or "").lower().strip()
        return u.startswith("rtsp://") or u.startswith("http://") or u.startswith("https://")

    # ---------- context menu ----------
    def on_table_context_menu(self, pos):
        row = self.table.indexAt(pos).row()
        if row < 0:
            return
        with STATE_LOCK:
            is_mosaic = 0 <= row < len(CHANNELS) and bool(CHANNELS[row].mosaic_sources)
        menu = QMenu(self)
        act_edit = menu.addAction("Edit…")
        if is_mosaic:
            act_edit_mosaic = menu.addAction("Edit Mosaic…")
        else:
            act_edit_mosaic = None
        act_remove = menu.addAction("Remove")
        act_check = menu.addAction("Check Status")
        act_edit_num = menu.addAction("Edit Channel Number…")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == act_edit:
            self.on_edit_row(row)
        elif chosen == act_remove:
            self.table.selectRow(row); self.on_remove()
        elif chosen == act_check:
            self.table.selectRow(row); self.on_check_status()
        elif chosen == act_edit_num:
            self.on_edit_number(row)
        elif act_edit_mosaic and chosen == act_edit_mosaic:
            self.on_edit_mosaic(row)

    def on_edit_number(self, row: int):
        with STATE_LOCK:
            if not (0 <= row < len(CHANNELS)):
                return
            ch = CHANNELS[row]
            old_id = str(ch.id)
        new_id, ok = QInputDialog.getText(self, "Edit Channel Number", "New channel number (1–99999):", text=old_id)
        if not ok:
            return
        new_id = (new_id or "").strip()
        if not new_id.isdigit() or not (1 <= int(new_id) <= 99999):
            QMessageBox.warning(self, "Invalid", "Please enter digits only, 1–99999.")
            return
        with STATE_LOCK:
            if any(c.id == new_id for c in CHANNELS) and new_id != old_id:
                QMessageBox.warning(self, "In Use", f"Channel {new_id} already exists.")
                return
            ch.id = new_id
        self._refresh_table()

    def on_edit_row(self, row: int):
        with STATE_LOCK:
            if not (0 <= row < len(CHANNELS)):
                return
            ch = CHANNELS[row]
        if ch.mosaic_sources:
            self.on_edit_mosaic(row)
            return
        url, oku = QInputDialog.getText(self, "Edit Source", "rtsp:// or https://...m3u8 URL:", text=ch.rtsp)
        if not oku or not url:
            return
        if not self._validate_source(url):
            QMessageBox.warning(self, "Invalid", "Enter rtsp:// or a valid HTTP(S) URL")
            return
        name, ok2 = QInputDialog.getText(self, "Edit Name", "Channel name:", text=ch.name or f"Channel {ch.id}")
        if not ok2:
            return
        try:
            idx_t = TRANSPORTS.index(ch.transport) if ch.transport in TRANSPORTS else 0
        except Exception:
            idx_t = 0
        transport, ok3 = QInputDialog.getItem(self, "RTSP Transport", "Transport (ignored for HLS):", TRANSPORTS, idx_t, False)
        if not ok3:
            transport = ch.transport
        try:
            idx_a = AUTH_MODES.index(ch.auth_mode) if ch.auth_mode in AUTH_MODES else 0
        except Exception:
            idx_a = 0
        auth_mode, ok4 = QInputDialog.getItem(self, "Auth Mode", "Send credentials:", AUTH_MODES, idx_a, False)
        if not ok4:
            auth_mode = ch.auth_mode
        username, _ = QInputDialog.getText(self, "Credentials", "Username:", text=ch.username or "")
        password, _ = QInputDialog.getText(self, "Credentials", "Password:", text=ch.password or "")
        xcode_default = 0 if ch.transcode_audio else 1
        xcode, ok5 = QInputDialog.getItem(self, "Audio Mode", "Audio:", ["Transcode AAC","Copy"], xcode_default, False)
        transcode_audio = ch.transcode_audio if not ok5 else (xcode == "Transcode AAC")
        with STATE_LOCK:
            ch.rtsp = url
            ch.name = name or ch.name
            ch.transport = transport
            ch.auth_mode = auth_mode
            ch.username = username or ""
            ch.password = password or ""
            ch.transcode_audio = bool(transcode_audio)
            ch.mosaic_sources = None
        self._refresh_table()
        self.on_check_status()

    def on_edit_mosaic(self, row: int):
        with STATE_LOCK:
            if not (0 <= row < len(CHANNELS)):
                return
            ch = CHANNELS[row]
        src_str, ok = QInputDialog.getText(self, "Edit Mosaic", "Comma-separated channel IDs (max 4):",
                                           text=",".join(ch.mosaic_sources or []))
        if not ok:
            return
        ids = [s.strip() for s in (src_str or "").split(",") if s.strip()]
        if len(ids) < 2:
            QMessageBox.warning(self, "Mosaic", "Enter at least two channel IDs.")
            return
        with STATE_LOCK:
            ch.mosaic_sources = ids[:4]
            ch.rtsp = f"mosaic://{','.join(ch.mosaic_sources)}"
        self._refresh_table()
        self.on_check_status()

    # ---------- top bar actions ----------
    def on_add(self):
        url, ok = QInputDialog.getText(self, "Add Source", "rtsp:// or https://... URL:")
        if not ok or not url:
            return
        if not self._validate_source(url):
            QMessageBox.warning(self, "Invalid", "Enter rtsp:// or a valid HTTP(S) URL")
            return
        ch_id = self._next_channel_id()
        name, ok2 = QInputDialog.getText(self, "Channel Name", f"Name for channel {ch_id}:")
        if not ok2:
            return
        transport, ok3 = QInputDialog.getItem(self, "RTSP Transport", "Choose transport (ignored for HLS):", TRANSPORTS, 0, False)
        if not ok3:
            transport = "Auto"
        auth_mode, ok4 = QInputDialog.getItem(self, "Auth Mode", "Send credentials:", AUTH_MODES, 0, False)
        if not ok4:
            auth_mode = "Auto"
        username, _ = QInputDialog.getText(self, "Credentials (optional)", "Username:")
        password, _ = QInputDialog.getText(self, "Credentials (optional)", "Password:")
        xcode, ok5 = QInputDialog.getItem(self, "Audio Mode", "Audio:", ["Transcode AAC","Copy"], 0, False)
        transcode_audio = (xcode == "Transcode AAC") if ok5 else True
        with STATE_LOCK:
            CHANNELS.append(Channel(ch_id, name or f"Channel {ch_id}", url, transport=transport,
                                    username=username or "", password=password or "",
                                    auth_mode=auth_mode, transcode_audio=transcode_audio))
        self._refresh_table()
        self.on_check_status()

    def on_add_mosaic(self):
        with STATE_LOCK:
            existing_ids = [c.id for c in CHANNELS]
        if len(existing_ids) < 2:
            QMessageBox.information(self, "Mosaic", "Add at least two channels first.")
            return
        src_str, ok = QInputDialog.getText(self, "Add Mosaic", "Enter comma-separated channel IDs (max 4):")
        if not ok:
            return
        ids = [s.strip() for s in (src_str or "").split(",") if s.strip()]
        if len(ids) < 2:
            QMessageBox.warning(self, "Mosaic", "Enter at least two channel IDs.")
            return
        ch_id = self._next_channel_id()
        name, ok2 = QInputDialog.getText(self, "Mosaic Name", f"Name for channel {ch_id}:", text=f"Mosaic {','.join(ids)}")
        if not ok2:
            return
        # Mosaic uses first source's audio transcode setting by default
        with STATE_LOCK:
            id_map = {c.id: c for c in CHANNELS}
            first = id_map.get(ids[0])
            transcode_audio = True if first is None else first.transcode_audio
            CHANNELS.append(Channel(ch_id, name or f"Mosaic {','.join(ids)}", f"mosaic://{','.join(ids)}",
                                    transport="Auto", transcode_audio=transcode_audio, mosaic_sources=ids[:4]))
        self._refresh_table()
        self.on_check_status()

    def on_remove(self):
        rows = self._selected_rows()
        if not rows:
            return
        with STATE_LOCK:
            for row in reversed(rows):
                if 0 <= row < len(CHANNELS):
                    CHANNELS.pop(row)
        self._refresh_table()

    def _set_status(self, row: int, status: str, detail: str):
        with STATE_LOCK:
            if 0 <= row < len(CHANNELS):
                CHANNELS[row].status = status
                CHANNELS[row].status_detail = detail
        if 0 <= row < self.table.rowCount():
            item = QTableWidgetItem(status)
            item.setToolTip(detail)
            self.table.setItem(row, 8, item)

    def on_probe_result(self, row: int, status: str, detail: str):
        self._set_status(row, status, detail)

    def on_check_status(self):
        with STATE_LOCK:
            chs = list(CHANNELS)
        if not (shutil.which(os.path.basename(FFPROBE)) or os.path.exists(FFPROBE)):
            for row, _ in enumerate(chs):
                self._set_status(row, "Error", "ffprobe not found")
            return
        for row, ch in enumerate(chs):
            self._set_status(row, "Checking...", "")
            worker = ProbeWorker(row, ch)
            worker.signals.result.connect(self.on_probe_result)
            self.threadpool.start(worker)

    def on_toggle_audio(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Audio", "Select a channel row first.")
            return
        row = rows[0]
        with STATE_LOCK:
            if 0 <= row < len(CHANNELS):
                CHANNELS[row].transcode_audio = not CHANNELS[row].transcode_audio
        self._refresh_table()

    def on_copy_url(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Copy URL", "Select a channel row first.")
            return
        port = int(self.port_spin.value())
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            host_ip = s.getsockname()[0]
        except Exception:
            host_ip = "127.0.0.1"
        finally:
            try:
                s.close()
            except Exception:
                pass
        ch_id = self.table.item(rows[0], 0).text()
        url = f"http://{host_ip}:{port}/auto/v{ch_id}"
        QApplication.clipboard().setText(url)
        QMessageBox.information(self, "Copy URL", f"Copied:\n{url}")

    def on_start_server(self):
        if not (shutil.which(os.path.basename(FFMPEG)) or os.path.exists(FFMPEG)):
            QMessageBox.warning(self, "ffmpeg missing", "ffmpeg not found. Place ffmpeg.exe next to the EXE or on PATH.")
            return
        SERVER_CFG["port"] = int(self.port_spin.value())
        if start_http_server():
            QMessageBox.information(self, "Server", f"Started on port {SERVER_CFG['port']}.")
        else:
            QMessageBox.warning(self, "Server", "Failed to start server. Is the port in use?")

    def on_stop_server(self):
        stop_http_server()
        QMessageBox.information(self, "Server", "Server stopped.")

    def on_save_config(self):
        if yaml is None:
            QMessageBox.warning(self, "YAML missing", "PyYAML is not installed; cannot save.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Config", "config.yaml", "YAML (*.yaml *.yml)")
        if not path:
            return
        with STATE_LOCK:
            data = {"server": SERVER_CFG.copy(), "channels": [c.to_dict() for c in CHANNELS]}
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            QMessageBox.information(self, "Saved", f"Saved to {path}")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to save: {e}")

    def on_load_config(self):
        if yaml is None:
            QMessageBox.warning(self, "YAML missing", "PyYAML is not installed; cannot load.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Load Config", "", "YAML (*.yaml *.yml)")
        if not path:
            return
        try:
            self.load_from_file(path)
            QMessageBox.information(self, "Loaded", f"Loaded {path}")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load: {e}")

    def load_from_file(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        server = data.get("server", {})
        chs = data.get("channels", [])
        with STATE_LOCK:
            SERVER_CFG.update({
                "name": server.get("name", SERVER_CFG["name"]),
                "host": server.get("host", SERVER_CFG["host"]),
                "port": int(server.get("port", SERVER_CFG["port"])),
                "device_id": server.get("device_id", SERVER_CFG.get("device_id"))
            })
            CHANNELS.clear()
            for d in chs:
                CHANNELS.append(Channel.from_dict(d))
        self.port_spin.setValue(SERVER_CFG["port"])
        self._refresh_table()
        self.on_check_status()

    def on_port_changed(self, val: int):
        SERVER_CFG["port"] = int(val)
        info = (f"Add to Plex (manual HDHR): http://<your-ip>:{val}\n"
                f"M3U:  http://<your-ip>:{val}/m3u\n"
                f"XMLTV: http://<your-ip>:{val}/xmltv\n"
                f"Direct test: http://<your-ip>:{val}/auto/v<channel>")
        self.info.setText(info)

    # ---------- DnD reorder (preserve numeric channel labels) ----------
    def reorder_rows(self, src: int, dst: int):
        with STATE_LOCK:
            n = len(CHANNELS)
            if not (0 <= src < n and 0 <= dst < n):
                return
            orig = CHANNELS[:]
            ids_before = [str(c.id) for c in orig]
            moved = orig.pop(src)
            orig.insert(dst, moved)  # new order
            # compute the contiguous span whose channel numbers will rotate
            lo, hi = (dst, src) if src > dst else (src, dst)
            span_ids = ids_before[lo:hi+1]
            # assign those ids to the span in the new order
            for i, cid in enumerate(span_ids, start=lo):
                orig[i].id = cid
            CHANNELS[:] = orig
        self._refresh_table()
        self.table.selectRow(dst)


# ================================= main() ====================================
def main():
    app_qt = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app_qt.exec())


if __name__ == "__main__":
    main()
