"""
CaseFlow v2.1 — Server startup script
Usage: python start_server.py

ต้องติดตั้ง cloudflared ก่อน:
  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

สร้าง Quick Tunnel (ไม่ต้องมีบัญชี):
  ไม่ต้องตั้งค่าอะไร — รันได้เลย

หรือใช้ Authenticated Tunnel (URL คงที่):
  ตั้ง CF_TUNNEL_TOKEN=<token> ใน .env
  สร้าง token ได้ที่ https://one.dash.cloudflare.com
"""

import os
import re
import sys
import subprocess
import threading

# ── Single-instance guard ─────────────────────────────────────────────────────
_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".start_server.pid")

def _check_single_instance():
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            import psutil
            if psutil.pid_exists(old_pid):
                print(f"[start_server] already running (PID {old_pid}). Exiting.")
                sys.exit(0)
        except Exception:
            pass
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(_PID_FILE) and os.remove(_PID_FILE))

_check_single_instance()
# ─────────────────────────────────────────────────────────────────────────────

# Ensure cloudflared is findable on Windows (winget installs to non-default PATH)
_CF_WIN_PATH = r"C:\Program Files (x86)\cloudflared"
if os.name == "nt" and _CF_WIN_PATH not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _CF_WIN_PATH + ";" + os.environ["PATH"]

from dotenv import load_dotenv
import uvicorn

load_dotenv()

public_url = None
_url_found = threading.Event()

_URL_PATTERN = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")


def _watch_cloudflared(proc: subprocess.Popen) -> None:
    global public_url
    for raw in proc.stdout:
        line = raw.strip()
        if not _url_found.is_set():
            m = _URL_PATTERN.search(line)
            if m:
                public_url = m.group(0)
                _url_found.set()


cf_token = os.getenv("CF_TUNNEL_TOKEN")

if cf_token:
    # Authenticated tunnel — domain is pre-configured in the Cloudflare dashboard
    cmd = ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", cf_token]
    public_url = os.getenv("CF_TUNNEL_HOSTNAME", "(ดู hostname ใน Cloudflare Dashboard)")
    _url_found.set()
else:
    # Quick Tunnel — Cloudflare generates a random *.trycloudflare.com URL
    cmd = ["cloudflared", "tunnel", "--no-autoupdate", "--url", "http://localhost:8000"]

proc = None
try:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
except FileNotFoundError:
    print("คำเตือน: ไม่พบ cloudflared — รันในโหมด Local Only (ไม่มี Public URL)")
    public_url = None
    _url_found.set()

if proc is not None:
    watcher = threading.Thread(target=_watch_cloudflared, args=(proc,), daemon=True)
    watcher.start()

    if not cf_token:
        print("รอ Cloudflare Tunnel พร้อม...")
        if not _url_found.wait(timeout=30):
            print("คำเตือน: ไม่พบ URL ภายใน 30 วินาที — ตรวจสอบว่า cloudflared ติดตั้งถูกต้อง")
            public_url = "(URL ไม่ถูกตรวจจับ)"

# ── เขียน URL ลงไฟล์เพื่อให้ bot อ่านได้ ─────────────────────────────────────
_CF_URL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cf_url")
with open(_CF_URL_FILE, "w") as _f:
    _f.write(public_url or "")
import atexit as _atexit
_atexit.register(lambda: os.path.exists(_CF_URL_FILE) and os.remove(_CF_URL_FILE))
# ─────────────────────────────────────────────────────────────────────────────

print("=" * 55)
print("  CaseFlow API Server")
print("=" * 55)
if public_url:
    print(f"  Public URL : {public_url}")
    print(f"  Mobile UI  : {public_url}/static/index.html")
    print(f"  API Docs   : {public_url}/docs")
else:
    print("  Public URL : (ไม่มี — ติดตั้ง cloudflared เพื่อเปิด tunnel)")
    print(f"  API Docs   : http://localhost:8000/docs")
print(f"  Local only : http://localhost:8000")
print("=" * 55)
print("กด Ctrl+C เพื่อหยุด server\n")

try:
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
finally:
    if proc is not None:
        proc.terminate()
