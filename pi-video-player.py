#!/usr/bin/env python3
"""
Stage Audio Works Raspberry Pi Video Player
- Loop video + 3 trigger videos (GPIO)
- VLC (cvlc) via HTTP interface
- Volume persistence
- Upload endpoints for loop, triggers, override
- Override mode: loops override video and ignores GPIO
- Web UI: status, controls, volume slider, uploads/downloads, branding
- UDP listener for remote commands
"""

import os
import time
import threading
import subprocess
import requests
import json
import signal
import sys
import RPi.GPIO as GPIO
import socket
from urllib.parse import quote
from typing import Optional
from flask import Flask, render_template_string, jsonify, request, send_from_directory, redirect, url_for
from xml.etree import ElementTree as ET

# ---------------- CONFIG ----------------
VIDEO_DIR = "/home/pi/videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

GPIO_PINS = [17, 27, 22]  # Trigger pins
TRIGGER_VIDEOS = {
    17: os.path.join(VIDEO_DIR, "trigger1.mp4"),
    27: os.path.join(VIDEO_DIR, "trigger2.mp4"),
    22: os.path.join(VIDEO_DIR, "trigger3.mp4"),
}

LOOP_VIDEO = os.path.join(VIDEO_DIR, "loop.mp4")
OVERRIDE_VIDEO_DEFAULT = os.path.join(VIDEO_DIR, "override.mp4")

VLC_HTTP_HOST = "http://localhost:8080"
VLC_PASSWORD = "yourpass"
VOLUME_FILE = "/home/pi/.vlc_volume"
CONFIG_FILE = "/home/pi/config.json"
BRANDING_IMAGE_PATH = "/home/pi/saw.jpg"
UPLOAD_FOLDER = VIDEO_DIR
ALLOWED_EXTENSIONS = {"mp4"}

UDP_PORT = 5505
UDP_COMMANDS = {
    "PLAY": "play",
    "PAUSE": "pause",
    "RESTART_LOOP": "restart",
    "OVERRIDE_ON": "override_on",
    "OVERRIDE_OFF": "override_off"
}

# ---------------- GLOBALS ----------------
app = Flask(__name__)
branding_image_exists = os.path.isfile(BRANDING_IMAGE_PATH)

vlc_process = None
current_video = os.path.basename(LOOP_VIDEO)
current_trigger_video = None
status_lock = threading.Lock()
vlc_lock = threading.Lock()
trigger_state_lock = threading.Lock()
trigger_running = False
restart_in_progress = False
paused_trigger = False

# ---------------- CONFIG LOAD/SAVE ----------------
def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        print("Error reading config:", e)
    return {"override_active": False, "override_video": OVERRIDE_VIDEO_DEFAULT}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception as e:
        print("Error saving config:", e)

config = load_config()
OVERRIDE_VIDEO = config.get("override_video", OVERRIDE_VIDEO_DEFAULT)
override_active = bool(config.get("override_active", False))

# ---------------- VLC HTTP HELPERS ----------------
def send_vlc_command(command):
    try:
        return requests.get(f"{VLC_HTTP_HOST}/requests/status.xml?command={command}", auth=("", VLC_PASSWORD), timeout=2)
    except Exception as e:
        print("VLC command error:", e)
        return None

def wait_for_vlc_http(timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{VLC_HTTP_HOST}/requests/status.xml", auth=("", VLC_PASSWORD), timeout=2)
            r.raise_for_status()
            return True
        except Exception:
            time.sleep(0.5)
    return False

def get_vlc_status():
    try:
        r = requests.get(f"{VLC_HTTP_HOST}/requests/status.xml", auth=("", VLC_PASSWORD), timeout=2)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        def _int(tag):
            el = root.find(tag)
            if el is None or el.text is None:
                return 0
            try:
                return int(el.text)
            except Exception:
                return 0
        state_el = root.find("state")
        filename = None
        # VLC exposes current media metadata under <information> in many builds.
        for info_el in root.findall(".//info"):
            if info_el.attrib.get("name") in ("filename", "title"):
                if info_el.text:
                    filename = info_el.text
                    break
        if filename:
            filename = os.path.basename(filename)
        loop_el = root.find("loop")
        repeat_el = root.find("repeat")
        currentplid_el = root.find("currentplid")
        return {
            "time": _int("time"),
            "length": _int("length"),
            "currentplid": int(currentplid_el.text) if currentplid_el is not None and currentplid_el.text and currentplid_el.text.isdigit() else None,
            "state": state_el.text if state_el is not None and state_el.text else "unknown",
            "volume": _int("volume"),
            "filename": filename,
            "loop": bool(int(loop_el.text)) if loop_el is not None and loop_el.text and loop_el.text.isdigit() else False,
            "repeat": bool(int(repeat_el.text)) if repeat_el is not None and repeat_el.text and repeat_el.text.isdigit() else False,
        }
    except Exception:
        return {"time": 0, "length": 0, "currentplid": None, "state": "unknown", "volume": 0, "filename": None, "loop": False, "repeat": False}

def get_vlc_volume():
    st = get_vlc_status()
    vol = st.get("volume")
    if vol is None:
        return 256
    try:
        return int(vol)
    except Exception:
        return 256

def set_vlc_volume(volume):
    try:
        v = max(0, min(512, int(volume)))
        requests.get(f"{VLC_HTTP_HOST}/requests/status.xml?command=volume&val={v}", auth=("", VLC_PASSWORD), timeout=2)
    except Exception as e:
        print("Error setting VLC volume:", e)

# ---------------- VLC PROCESS ----------------
def _file_uri(path: str) -> str:
    # VLC HTTP expects a URI; path may contain spaces.
    return "file:///" + quote(os.path.abspath(path).lstrip("/"))

def ensure_vlc_running(initial_path: Optional[str] = None) -> bool:
    global vlc_process
    with vlc_lock:
        if vlc_process is not None and vlc_process.poll() is None and wait_for_vlc_http(timeout=1.5):
            return True

        # Best-effort cleanup of a prior VLC instance we own.
        if vlc_process is not None:
            try:
                vlc_process.terminate()
                vlc_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    vlc_process.kill()
                except Exception:
                    pass
            finally:
                vlc_process = None

        cmd = [
            "cvlc",
            "--fullscreen",
            "--no-video-title-show",
            "--extraintf",
            "http",
            "--http-password",
            VLC_PASSWORD,
            "--avcodec-hw=any",
        ]
        if initial_path:
            cmd.append(initial_path)

        try:
            vlc_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("Failed to start cvlc:", e)
            vlc_process = None
            return False

    return wait_for_vlc_http(timeout=10)

def set_vlc_repeat(desired: bool) -> None:
    st = get_vlc_status()
    if bool(st.get("repeat", False)) != bool(desired):
        send_vlc_command("pl_repeat")

def set_vlc_loop(desired: bool) -> None:
    st = get_vlc_status()
    if bool(st.get("loop", False)) != bool(desired):
        send_vlc_command("pl_loop")

def vlc_play_file(path: str, *, repeat: bool) -> None:
    """
    Play a file in the existing VLC instance without restarting the process.
    repeat=True is used for the base loop/override video; triggers should use repeat=False.
    """
    global current_video
    if not ensure_vlc_running(initial_path=path):
        # VLC didn't come up; state still updates for UI, but playback won't.
        with status_lock:
            current_video = os.path.basename(path)
        return

    # Avoid playlist looping surprises: for our use-case we rely on repeat (not playlist loop).
    set_vlc_loop(False)
    set_vlc_repeat(bool(repeat))
    send_vlc_command(f"in_play&input={_file_uri(path)}")
    with status_lock:
        current_video = os.path.basename(path)

def vlc_enqueue_file(path: str) -> None:
    if ensure_vlc_running():
        send_vlc_command(f"in_enqueue&input={_file_uri(path)}")

def get_vlc_playlist_items():
    try:
        r = requests.get(f"{VLC_HTTP_HOST}/requests/playlist.xml", auth=("", VLC_PASSWORD), timeout=2)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        items = []
        for leaf in root.findall(".//leaf"):
            items.append(
                {
                    "id": leaf.attrib.get("id"),
                    "name": leaf.attrib.get("name"),
                    "uri": leaf.attrib.get("uri"),
                }
            )
        return items
    except Exception:
        return []

def delete_playlist_entries_by_name(basename: str) -> None:
    for item in get_vlc_playlist_items():
        if not item.get("id"):
            continue
        name = item.get("name") or ""
        uri = item.get("uri") or ""
        if os.path.basename(name) == basename or os.path.basename(uri) == basename:
            send_vlc_command(f"pl_delete&id={item['id']}")

def cleanup_playlist_keep_current(keep_basename: str) -> None:
    """
    Prevent unbounded playlist growth when enqueuing triggers/base.
    Keeps the currently playing entry (if known) and removes other entries that match keep_basename.
    """
    st = get_vlc_status()
    keep_id = st.get("currentplid")
    for item in get_vlc_playlist_items():
        item_id = item.get("id")
        if not item_id:
            continue
        if keep_id is not None and str(keep_id) == str(item_id):
            continue
        name = item.get("name") or ""
        uri = item.get("uri") or ""
        if os.path.basename(name) == keep_basename or os.path.basename(uri) == keep_basename:
            send_vlc_command(f"pl_delete&id={item_id}")

def get_base_video_path() -> Optional[str]:
    if override_active and OVERRIDE_VIDEO and os.path.exists(OVERRIDE_VIDEO):
        return OVERRIDE_VIDEO
    if os.path.exists(LOOP_VIDEO):
        return LOOP_VIDEO
    return None

def play_base_loop() -> None:
    base = get_base_video_path()
    if not base:
        return
    vlc_play_file(base, repeat=True)

def wait_for_playback_end(poll_interval=0.25, safety_timeout=24*3600):
    start = time.time()
    while True:
        st = get_vlc_status()
        if st.get("state") != "playing": return
        length = st.get("length",0); elapsed = st.get("time",0)
        if length>0 and elapsed>=length-1: return
        if time.time()-start> safety_timeout: print("Safety timeout waiting for playback end"); return
        time.sleep(poll_interval)

# ---------------- Volume persistence ----------------
def save_volume(vol):
    try: open(VOLUME_FILE,"w").write(str(int(vol)))
    except Exception as e: print("Error saving volume:",e)

def load_saved_volume():
    try:
        if os.path.exists(VOLUME_FILE): return int(open(VOLUME_FILE,"r").read().strip())
    except Exception as e: print("Error loading saved volume:",e)
    return None

# ---------------- Upload helpers ----------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ---------------- UDP LISTENER ----------------
def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))
    print(f"UDP listener running on port {UDP_PORT}")
    global override_active, restart_in_progress, paused_trigger
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            cmd = data.decode("utf-8").strip().upper()
            if cmd not in UDP_COMMANDS: continue
            action = UDP_COMMANDS[cmd]
            if action=="play":
                if paused_trigger and current_trigger_video:
                    send_vlc_command("pl_forceresume")
                    paused_trigger = False
                else:
                    send_vlc_command("pl_forceresume")
            elif action=="pause":
                if trigger_running and current_trigger_video:
                    send_vlc_command("pl_forcepause")
                    paused_trigger = True
                else:
                    send_vlc_command("pl_forcepause")
            elif action=="restart":
                restart_in_progress = True
                try:
                    play_base_loop()
                finally:
                    restart_in_progress = False
            elif action=="override_on":
                override_active=True
                config['override_active']=True
                save_config(config)
                play_base_loop()
            elif action=="override_off":
                override_active=False
                config['override_active']=False
                save_config(config)
                play_base_loop()
        except Exception as e:
            print("UDP listener error:", e)

# ---------------- GPIO TRIGGER ----------------
GPIO.setmode(GPIO.BCM)
for pin in GPIO_PINS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def trigger_worker(pin):
    while True:
        if GPIO.input(pin)==GPIO.LOW:
            play_trigger(pin)
        time.sleep(0.1)

for pin in GPIO_PINS:
    t=threading.Thread(target=trigger_worker,args=(pin,),daemon=True)
    t.start()

def play_trigger(pin):
    global trigger_running, current_trigger_video, paused_trigger
    with trigger_state_lock:
        if trigger_running or override_active or restart_in_progress:
            return
    video_path = TRIGGER_VIDEOS.get(pin)
    if not video_path or not os.path.exists(video_path): return
    base_path = LOOP_VIDEO if os.path.exists(LOOP_VIDEO) else None
    with trigger_state_lock:
        trigger_running = True
        current_trigger_video = os.path.basename(video_path)
        paused_trigger = False
    try:
        # Smooth transition: enqueue trigger and the base loop, then skip to next item.
        # We temporarily disable repeat so the trigger can't loop.
        if not ensure_vlc_running(initial_path=base_path or video_path):
            vlc_play_file(video_path, repeat=False)
            wait_for_playback_end()
        else:
            set_vlc_repeat(False)
            vlc_enqueue_file(video_path)
            if base_path:
                vlc_enqueue_file(base_path)
            send_vlc_command("pl_next")

            trigger_name = os.path.basename(video_path)
            # Wait until the trigger is actually playing...
            start = time.time()
            while time.time() - start < 5:
                st = get_vlc_status()
                if st.get("filename") == trigger_name:
                    break
                time.sleep(0.1)
            # ...then wait until playback moves off the trigger.
            start = time.time()
            while time.time() - start < 10 * 60:
                st = get_vlc_status()
                if st.get("filename") and st.get("filename") != trigger_name:
                    break
                time.sleep(0.1)

            # Ensure the base loop repeats going forward.
            set_vlc_repeat(True)
            delete_playlist_entries_by_name(trigger_name)
            if base_path:
                cleanup_playlist_keep_current(os.path.basename(base_path))
    finally:
        with trigger_state_lock:
            trigger_running = False
            current_trigger_video = None
            paused_trigger = False
        if not restart_in_progress:
            play_base_loop()

# ---------------- FLASK HTML TEMPLATE ----------------
HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<title>Stage Audio Works Raspberry Pi Video Player</title>
<meta charset="utf-8">
<style>
body { font-family: Arial; margin:20px; background:#f7f7f7; color:#222; }
h1 { font-size:1.8em; margin-bottom:6px; }
.status { margin-bottom:12px; }
.buttons button { margin-right:8px; padding:8px 14px; font-size:1em; border-radius:6px; border:none; background:#007bff; color:white; cursor:pointer; }
.upload-section { margin-top:18px; padding:14px; background:#fff; border-radius:8px; border:1px solid #ddd; }
.upload-section form { margin-bottom:8px; }
label.inline { display:inline-block; width:160px; }
.branding { margin-top:22px; text-align:center; color:#555; border-top:1px solid #ddd; padding-top:12px; }
.branding img { max-height:60px; margin-top:8px; }
input[type=range] { width:320px; vertical-align: middle; margin-left:10px; }
.override { margin-top:14px; }
.downloads { margin-top:12px; padding:8px; background:#eef; border-radius:6px; }
</style>
<script>
async function fetchStatus() {
  const res = await fetch('/status');
  const data = await res.json();
  document.getElementById('video').textContent = data.video;
  document.getElementById('state').textContent = data.state;
  document.getElementById('position').textContent = data.position;
  document.getElementById('remaining').textContent = data.remaining;
  document.getElementById('overrideCheckbox').checked = !!data.override_active;
}
async function fetchVolume() {
  const res = await fetch('/volume');
  const json = await res.json();
  const slider = document.getElementById('volumeSlider');
  slider.value = json.volume;
  document.getElementById('volumeValue').textContent = Math.round((json.volume / 512) * 100)+'%';
}
function setVolume(value) {
  fetch('/volume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({volume:parseInt(value)})});
  document.getElementById('volumeValue').textContent = Math.round((value / 512) * 100)+'%';
}
async function setOverride(active) {
  await fetch('/set_override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:!!active})});
  fetchStatus();
}
setInterval(fetchStatus,1000);
setInterval(fetchVolume,5000);
window.onload=function(){ fetchStatus(); fetchVolume(); }
async function playVideo(){ await fetch('/play',{ method:'POST' }); fetchStatus(); }
async function pauseVideo(){ await fetch('/pause',{ method:'POST' }); fetchStatus(); }
async function restartLoop(){ await fetch('/restart',{ method:'POST' }); fetchStatus(); }
</script>
</head>
<body>
<h1>Stage Audio Works Raspberry Pi Video Player</h1>
<div class="status">
<strong>Current Video:</strong> <span id="video">Loading...</span><br>
<strong>Status:</strong> <span id="state">Loading...</span><br>
<strong>Current Time:</strong> <span id="position">--:--:--</span><br>
<strong>Time Left:</strong> <span id="remaining">--:--:--</span>
</div>
<div class="buttons">
<button onclick="playVideo()">▶ Play</button>
<button onclick="pauseVideo()">⏸ Pause</button>
<button onclick="restartLoop()">🔄 Restart Loop</button>
</div>
<div class="volume-control" style="margin-top:12px;">
<label for="volumeSlider">Volume:</label>
<input type="range" id="volumeSlider" min="0" max="512" step="1" oninput="setVolume(this.value)">
<span id="volumeValue">--%</span>
</div>
<div class="upload-section">
<h3>Manage Videos</h3>
<form method="POST" action="/upload/loop" enctype="multipart/form-data">
<label class="inline">Loop Video:</label><input type="file" name="file" accept="video/mp4" required><button type="submit">Upload</button>
</form>
<form method="POST" action="/upload/trigger/1" enctype="multipart/form-data">
<label class="inline">Trigger 1:</label><input type="file" name="file" accept="video/mp4" required><button type="submit">Upload</button>
</form>
<form method="POST" action="/upload/trigger/2" enctype="multipart/form-data">
<label class="inline">Trigger 2:</label><input type="file" name="file" accept="video/mp4" required><button type="submit">Upload</button>
</form>
<form method="POST" action="/upload/trigger/3" enctype="multipart/form-data">
<label class="inline">Trigger 3:</label><input type="file" name="file" accept="video/mp4" required><button type="submit">Upload</button>
</form>
<hr style="margin:12px 0;">
<form method="POST" action="/upload/override" enctype="multipart/form-data">
<label class="inline">Override Video:</label><input type="file" name="file" accept="video/mp4" required><button type="submit">Upload Override</button>
</form>
<div class="override"><label><input type="checkbox" id="overrideCheckbox" onchange="setOverride(this.checked)"> Activate Override</label></div>
<div class="downloads">
<strong>Download Current Videos:</strong><br>
<a href="/download/loop">⬇ Loop</a> |
<a href="/download/trigger/1">⬇ Trigger1</a> |
<a href="/download/trigger/2">⬇ Trigger2</a> |
<a href="/download/trigger/3">⬇ Trigger3</a> |
<a href="/download/override">⬇ Override</a>
</div>
<div class="branding">A Stage Audio Works Innovation
{% if branding_image %}<br><a href="https://stageaudioworks.com" target="_blank" rel="noopener noreferrer"><img src="/branding-image" alt="Stage Audio Works Logo"></a>{% endif %}</div>
</body>
</html>
"""

# ---------------- FLASK ROUTES ----------------
@app.route('/')
def index(): return render_template_string(HTML_TEMPLATE, branding_image=branding_image_exists)

@app.route('/branding-image')
def branding_image(): return send_from_directory(os.path.dirname(BRANDING_IMAGE_PATH), os.path.basename(BRANDING_IMAGE_PATH)) if branding_image_exists else ("",404)

@app.route('/play', methods=["POST"])
def play(): 
    global paused_trigger
    with trigger_state_lock:
        is_trigger = bool(trigger_running and current_trigger_video)
        is_paused_trigger = bool(paused_trigger and is_trigger)
    if is_paused_trigger:
        send_vlc_command("pl_forceresume")
        with trigger_state_lock:
            paused_trigger = False
    else:
        send_vlc_command("pl_forceresume")
    return "",204

@app.route('/pause', methods=["POST"])
def pause(): 
    global paused_trigger
    with trigger_state_lock:
        is_trigger = bool(trigger_running and current_trigger_video)
    if is_trigger:
        send_vlc_command("pl_forcepause")
        with trigger_state_lock:
            paused_trigger = True
    else:
        send_vlc_command("pl_forcepause")
    return "",204

@app.route('/restart', methods=["POST"])
def restart():
    global restart_in_progress
    restart_in_progress=True
    try:
        play_base_loop()
    finally:
        restart_in_progress=False
    return "",204

@app.route('/status')
def status():
    st=get_vlc_status()
    pos=st.get("time",0); length=st.get("length",0)
    with trigger_state_lock:
        is_trigger = bool(trigger_running)
        trigger_name = current_trigger_video
    return jsonify({
        "video": trigger_name if is_trigger else current_video,
        "state": st.get("state","unknown"),
        "position": time.strftime("%H:%M:%S", time.gmtime(pos)),
        "remaining": time.strftime("%H:%M:%S", time.gmtime(length-pos if length-pos>0 else 0)),
        "override_active": override_active,
        "volume": st.get("volume",256)
    })

@app.route('/volume', methods=["GET","POST"])
def volume():
    if request.method=="GET":
        return jsonify({"volume": get_vlc_volume()})
    else:
        data=request.get_json()
        vol=int(data.get("volume",256))
        set_vlc_volume(vol)
        save_volume(vol)
        return "",204

@app.route('/set_override', methods=["POST"])
def set_override():
    global override_active
    data=request.get_json()
    active=bool(data.get("active",False))
    override_active=active
    config['override_active']=active
    save_config(config)
    play_base_loop()
    return "",204

@app.route('/upload/<video_type>', methods=["POST"])
@app.route('/upload/<video_type>/<int:trigger_id>', methods=["POST"])
def upload(video_type, trigger_id=None):
    if 'file' not in request.files: return "No file",400
    f=request.files['file']
    if f.filename=="" or not allowed_file(f.filename): return "Invalid file",400
    if video_type=="loop": path=LOOP_VIDEO
    elif video_type=="override": path=OVERRIDE_VIDEO
    elif video_type=="trigger" and trigger_id in [1,2,3]: path=TRIGGER_VIDEOS[GPIO_PINS[trigger_id-1]]
    else: return "Unknown type",400
    f.save(path)
    return redirect(url_for('index'))

@app.route('/download/<video_type>')
@app.route('/download/<video_type>/<int:trigger_id>')
def download(video_type, trigger_id=None):
    if video_type=="loop": path=LOOP_VIDEO
    elif video_type=="override": path=OVERRIDE_VIDEO
    elif video_type=="trigger" and trigger_id in [1,2,3]: path=TRIGGER_VIDEOS[GPIO_PINS[trigger_id-1]]
    else: return "Unknown type",404
    if os.path.exists(path): return send_from_directory(os.path.dirname(path), os.path.basename(path), as_attachment=True)
    return "File not found",404

# ---------------- STARTUP ----------------
saved_vol = load_saved_volume()
if saved_vol is not None: set_vlc_volume(saved_vol)

play_base_loop()

udp_thread = threading.Thread(target=udp_listener, daemon=True)
udp_thread.start()

try:
    app.run(host="0.0.0.0", port=5000)
finally:
    GPIO.cleanup()
