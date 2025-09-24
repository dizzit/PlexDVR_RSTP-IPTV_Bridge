# PlexDVR_RSTP-IPTV_Bridge
Turn IP cameras (RTSP/HLS) into Plex Live TV channels with an HDHomeRun-compatible bridge and a simple PyQt6 GUI.

# Plex Camera IPTV Gateway

Turn IP cameras (**RTSP**) and **HLS `.m3u8` IPTV** streams into Plex **Live TV/DVR** channels.
The app emulates an **HDHomeRun** tuner, serves **M3U** and **XMLTV** endpoints, and uses **FFmpeg** to remux/transcode as needed. A simple **PyQt6 GUI** lets you add/edit channels, drag-and-drop reorder with slot-takeover renumbering, and run quick health checks.

> **Tested with camera:** **TP-Link Tapo C520WS** (RTSP).
> Bonus: you can also add **`.m3u8` IPTV channels** as regular channels.

---

## ✨ Capabilities

* **Plex tuner bridge (HDHomeRun-compatible)**

  * `/discover.json`, `/lineup.json`, `/auto/v<channel>`
  * Works with Plex “Set up DVR → Enter manually”
* **M3U & XMLTV endpoints**

  * `http://<host>:<port>/m3u`
  * `http://<host>:<port>/xmltv?hours=24&slot=30`
* **GUI channel manager (PyQt6)**

  * Add RTSP/HLS streams (per-channel **auth**, **custom HTTP headers**)
  * **Drag & drop** rows with **slot-takeover renumbering** (mosaics auto-remap)
  * Right-click: **Edit…**, **Edit Channel Number…**, **Remove**, **Check Status**
  * Per-channel **Audio**: **Transcode to AAC** or **Copy** (skip transcode if cam already outputs AAC)
* **Mosaic channel** (2–4 tiles) composed of other channels
* **Stability helpers**

  * **FFprobe** health check button
  * Short **FFmpeg warm-up** after Add/Edit (HLS by default; RTSP skipped to avoid single-session lockouts)
  * **Auto-restart on drop** (exponential backoff)
* **Simple DVR helpers**

  * **Record Now** / **Stop Recording**
  * Daily **schedule block** (start time & duration; TS/MP4, selectable directory)
* **Smart HLS resolver**

  * If you paste a non-`.m3u8` HTTP URL, the app will try `/index.m3u8`, `/master.m3u8`, `/playlist.m3u8`

---

## 🖥️ Requirements

* **Windows 10/11**
* **Python 3.11+** (works on 3.13 as well)
* **FFmpeg & FFprobe** binaries available (same folder as the app/exe or on PATH)
* Python packages: `PyQt6`, `Flask`, `PyYAML`

Install deps:

```bash
pip install PyQt6 Flask PyYAML
```

---

## 🚀 Quick Start

1. **Run the app** (`python plex_cam_gateway_gui_*.py`)
2. Click **Add** and enter:

   * **RTSP** (e.g. Tapo C520WS): `rtsp://<cam-ip>:554/stream1`

     * Set **Transport** (Auto/TCP/UDP) as needed
     * Provide **username/password** (the app URL-encodes special chars)
   * **HLS .m3u8**: paste the `.m3u8` URL or a page URL (the app probes common playlist names)
   * If the stream needs **headers** (e.g., `Referer`, `Cookie`), add them in **Custom Headers**
3. **Start Server** (default port: **8000**)
4. In Plex: **Live TV & DVR → Set up Plex DVR → Enter manually**

   * Enter: `http://<your-ip>:8000`
   * EPG (optional): `http://<your-ip>:8000/xmltv?hours=48&slot=30`
5. Watch your new channels in Plex.
   Test directly: `http://<your-ip>:8000/auto/v101`

---

## 📡 Endpoints

* **M3U:** `http://<host>:<port>/m3u`
* **XMLTV:** `http://<host>:<port>/xmltv?hours=24&slot=30`
* **HDHR stream:** `http://<host>:<port>/auto/v<channel>`
* **Diag (ffprobe):** `http://<host>:<port>/diag/<channel>`

---

## 🧱 Example Config (YAML)

```yaml
server:
  name: CamIPTV
  host: 0.0.0.0
  port: 8000

channels:
  - id: "101"
    name: "Driveway (Tapo C520WS)"
    rtsp: "rtsp://192.168.68.146:554/stream1"
    transport: "TCP"
    username: "yourUser"
    password: "Your#Pass123"
    auth_mode: "Auto"
    transcode_audio: true            # true = AAC, false = copy
    tvg_id: "cam.101"
    tvg_logo: ""
    epg_title: "Driveway Cam"
    epg_desc: "Live camera feed"

  - id: "102"
    name: "News (HLS)"
    rtsp: "https://example.com/live/index.m3u8"
    transport: "Auto"
    auth_mode: "Auto"
    headers: |
      Referer: https://example.com/
      User-Agent: Mozilla/5.0
    transcode_audio: true

  # Mosaic across two channels:
  - id: "103"
    name: "Front + Driveway"
    rtsp: ""               # empty for mosaics
    sources: ["101","102"] # compose from existing channels
    transcode_audio: true
```

> If your camera password contains special characters (e.g. `#`), the app’s **Auto** RTSP auth path will URL-encode them correctly.

---

## 🛠️ Build a Windows EXE (PyInstaller)

Put `ffmpeg.exe` and `ffprobe.exe` **next to** your `.py` file (or change paths), then:

```powershell
pyinstaller --clean --noconfirm --name PlexCamGateway --collect-all PyQt6 --add-binary "ffmpeg.exe;." --add-binary "ffprobe.exe;." plex_cam_gateway_gui_v7_9a_compile_ready.py
```

Run the built EXE from a console first to see logs the very first time (helps with firewall prompts).

---

## 🧩 Notes on Tapo C520WS

* Typical RTSP path: `rtsp://<cam-ip>:554/stream1`
* Many RTSP cams allow **one client at a time**. Close VLC/other viewers while Plex is tuned.
* If the camera stutters on UDP, try **Transport = TCP**.

---

## ➕ Adding `.m3u8` IPTV Channels

* Paste a direct `.m3u8` URL.
* If you only have a landing page URL, the app tries common playlist names (`/index.m3u8`, `/master.m3u8`, `/playlist.m3u8`).
* Add required **headers** (e.g., `Referer`, `Cookie`) in **Custom Headers** so both **ffprobe** and **ffmpeg** use them.

> ⚠️ Ensure you have the legal right to view/redistribute any IPTV streams you add.

---

## 🔍 Troubleshooting

* **“Could not tune channel” in Plex**

  * Confirm you can play `http://<ip>:<port>/auto/v<channel>` in a desktop player (e.g., VLC).
  * Check that no other app (including VLC) is connected to the same RTSP camera.
  * Try Transport **TCP** for RTSP, and verify credentials.
* **EXE opens/closes instantly**

  * Run it from **PowerShell/CMD** to see errors.
  * Ensure `ffmpeg.exe` and `ffprobe.exe` are present (or on PATH).
* **Audio issues**

  * If your cam already outputs **AAC**, set Audio to **Copy** to avoid unnecessary transcode.
* **HLS requires headers**

  * Add `Referer`, `User-Agent`, or `Cookie` in the channel’s Custom Headers dialog.

---

## 🔐 Privacy & Security

* Streams stay **local** (your machine runs the tuner/web endpoints).
* If you expose the port outside your LAN, use appropriate network controls (VPN, reverse proxy, firewall).

---

## 📄 License

FREE

---

## 🏷️ Release & Tag

Suggested release title: **PlexCamGateway v7.9a — Drag-and-Drop, Warm-Up, Mosaic**
Tag name: **`v7.9a`**

---

Happy channeling! If you want badges, screenshots, or a demo GIF, drop them in `docs/` and I’ll wire them into the README.
