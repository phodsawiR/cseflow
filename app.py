"""
CaseFlow Desktop App
รันด้วย: python app.py
"""
import os
import sys
import threading
import time
import urllib.request
import urllib.error

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

_PORT = 8000
_URL  = f"http://127.0.0.1:{_PORT}/static/index.html"


# ── Start FastAPI server in background thread ─────────────────────────────────

def _start_server():
    import uvicorn
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=_PORT,
        reload=False,
        log_level="warning",   # ลด noise ใน console
    )


def _wait_for_server(timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{_PORT}/exam_kb_status", timeout=1
            )
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import webview

    # Start server
    t = threading.Thread(target=_start_server, daemon=True)
    t.start()

    # Splash window while server loads
    splash = webview.create_window(
        "CaseFlow — กำลังเริ่มต้น...",
        html="""
        <html><body style="
            margin:0; display:flex; align-items:center; justify-content:center;
            height:100vh; background:#1e3a5f; font-family:'Segoe UI',sans-serif; color:#fff;
            flex-direction:column; gap:16px;">
          <div style="font-size:32px; font-weight:700; letter-spacing:-1px;">CaseFlow</div>
          <div style="font-size:14px; opacity:.7;">กำลังเริ่มต้น server...</div>
          <div style="width:180px; height:4px; background:#ffffff22; border-radius:4px; overflow:hidden; margin-top:8px;">
            <div id="bar" style="height:100%; background:#60a5fa; border-radius:4px; width:0%;
                 animation: load 10s linear forwards;">
            </div>
          </div>
          <style>
            @keyframes load { to { width: 90%; } }
          </style>
        </body></html>
        """,
        width=380, height=240,
        resizable=False,
        frameless=True,
    )

    def _on_server_ready():
        ready = _wait_for_server(timeout=20)
        if not ready:
            webview.windows[0].set_title("CaseFlow — Server ไม่ตอบสนอง")
            return

        # Open main window แล้วปิด splash
        main_win = webview.create_window(
            "CaseFlow",
            _URL,
            width=1280,
            height=820,
            min_size=(900, 600),
        )

        def _close_splash():
            time.sleep(0.5)
            try:
                splash.destroy()
            except Exception:
                pass

        threading.Thread(target=_close_splash, daemon=True).start()

    threading.Thread(target=_on_server_ready, daemon=True).start()

    webview.start(debug=False)


if __name__ == "__main__":
    main()
