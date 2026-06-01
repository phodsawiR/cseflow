import telebot
import subprocess
import shutil
import time
import re
import os
import sys
import json
import threading
import requests as _req
from dotenv import load_dotenv as _load_dotenv
from gap_finder import find_missing_topics, format_gap_report, classify_topic

_load_dotenv()

# ── Gemini client for /ward extraction ───────────────────────────────────────
try:
    from google import genai as _genai
    _gemini = _genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    _GEMINI_OK = True
except Exception:
    _GEMINI_OK = False

# ── Single-instance guard ─────────────────────────────────────────────────────
_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".obsidian_agent.pid")

def _pid_alive(pid: int) -> bool:
    """Check if a PID is alive without psutil — uses tasklist on Windows."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def _check_single_instance():
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            if _pid_alive(old_pid):
                print(f"[obsidian_agent] already running (PID {old_pid}). Exiting.")
                sys.exit(0)
        except Exception:
            pass  # stale or unreadable PID file — continue
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(_PID_FILE) and os.remove(_PID_FILE))

_check_single_instance()
# ─────────────────────────────────────────────────────────────────────────────

# ==========================================
# 1. CONFIGURATION
# ==========================================
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
ALLOWED_USER_ID = int(os.getenv('TELEGRAM_USER_ID', '0'))
CASEFLOW_PATH = r"C:\Users\USER\OneDrive\Desktop\caseflow"
VAULT_PATH = os.path.join(CASEFLOW_PATH, "obsidian")
GEMINI_CHAT_MODEL = "gemini-2.5-flash"

if not API_TOKEN:
    print("[ERROR] TELEGRAM_BOT_TOKEN not set in .env")
    sys.exit(1)

bot = telebot.TeleBot(API_TOKEN)

# ── Ward buzzword extractor ───────────────────────────────────────────────────

def _extract_ward_buzzwords(text: str) -> list[str]:
    """ใช้ Gemini สกัด medical topics จากใบส่งเวร → list of topic strings."""
    if not _GEMINI_OK:
        return []
    prompt = (
        "You are a medical knowledge base curator for a Thai medical student's Obsidian vault.\n"
        "Extract all distinct medical topics from this patient handover note that deserve individual notes.\n\n"
        "Include: diseases, syndromes, specific conditions, named organisms, specific drug names.\n"
        "Exclude: patient names, room numbers, HN numbers, dates, staff names, generic words (fever, plan, improved).\n\n"
        "Rules:\n"
        "- Use standard English medical terminology\n"
        "- Keep names concise (2-5 words)\n"
        "- Expand abbreviations where known (e.g. AVNRT → AVNRT, CTEPH → CTEPH)\n"
        "- Do NOT include dose numbers or lab values\n\n"
        "Return ONLY a JSON array of strings. No explanation.\n\n"
        f"Handover:\n{text[:4000]}"
    )
    try:
        resp = _gemini.models.generate_content(model="gemini-3.5-flash", contents=prompt)
        raw = resp.text.strip()
        m = re.search(r'\[.*?\]', raw, re.DOTALL)
        if m:
            return [t.strip() for t in json.loads(m.group(0)) if isinstance(t, str) and t.strip()]
    except Exception as e:
        print(f"[ward] extraction error: {e}")
    return []


def _filter_new_topics(topics: list[str]) -> list[str]:
    """กรองเฉพาะ topics ที่ยังไม่มี note ใน vault."""
    existing: set[str] = set()
    if os.path.exists(VAULT_PATH):
        for root, dirs, files in os.walk(VAULT_PATH):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if fname.endswith(".md"):
                    existing.add(fname[:-3].lower().replace("_", " ").strip())
    return [t for t in topics if t.lower().replace("_", " ").strip() not in existing]


# ── CaseFlow API ──────────────────────────────────────────────────────────────
CF_API = "http://localhost:8000"

_BRANCH_LABELS = {
    "A": "Case Analysis",   "B": "Knowledge Query",
    "C": "Symptom Approach","D": "Progress Note",
    "E": "Morning Round",   "F": "Interpreter",
    "G": "Admission Note",  "U": "Freestyle",
}

# user_id -> {"session_id": str, "state": "processing|waiting_confirm|done"}
cf_state: dict[str, dict] = {}


def _cf_send(chat_id: int, text: str):
    """Send long text in ≤4000-char chunks, Markdown with plain fallback."""
    chunks = [text[i:i+4000] for i in range(0, max(len(text), 1), 4000)]
    for chunk in chunks:
        try:
            bot.send_message(chat_id, chunk, parse_mode="Markdown")
        except Exception:
            try:
                bot.send_message(chat_id, chunk)
            except Exception:
                pass


_STEP_ICON = {"done": "✅", "running": "⚙️", "error": "❌", "waiting_input": "⏸️"}


def _build_progress_text(steps: list[dict], elapsed: int, done: bool = False) -> str:
    header = f"{'✅ เสร็จแล้ว' if done else '⏳ กำลังทำงาน'} ({elapsed}s)\n"
    lines = [header]
    for s in steps:
        icon   = _STEP_ICON.get(s.get("status", ""), "•")
        label  = s.get("label", s.get("agent", "?"))
        detail = s.get("detail", "")
        suffix = f" — {detail[:80]}" if detail and s.get("status") in ("error", "done") else ""
        if s.get("status") == "running":
            lines.append(f"{icon} {label}...")
        else:
            lines.append(f"{icon} {label}{suffix}")
    # ถ้ายาวเกิน 3800 ให้เก็บแค่ header + N ล่าสุด
    text = "\n".join(lines)
    if len(text) > 3800:
        kept = [header]
        for line in lines[1:][-25:]:   # 25 steps ล่าสุด
            kept.append(line)
        text = "\n".join(kept)
    return text


def _poll_and_deliver(user_id: str, chat_id: int, progress_msg_id: int):
    """Background thread: poll /result + /progress, update live step log, deliver draft."""
    info = cf_state.get(user_id)
    if not info:
        return
    session_id = info["session_id"]
    start      = time.time()
    last_text  = ""

    while True:
        time.sleep(4)

        # ── ดึง result ─────────────────────────────────────────────────────
        try:
            data = _req.get(f"{CF_API}/result/{session_id}", timeout=15).json()
        except Exception as e:
            try:
                bot.edit_message_text(
                    f"❌ ติดต่อ CaseFlow ไม่ได้\n`{str(e)[:200]}`",
                    chat_id, progress_msg_id, parse_mode="Markdown",
                )
            except Exception:
                pass
            cf_state.pop(user_id, None)
            return

        # ── ดึง progress steps ─────────────────────────────────────────────
        steps: list[dict] = []
        try:
            prog  = _req.get(f"{CF_API}/progress/{session_id}", timeout=5).json()
            steps = prog.get("steps", [])
        except Exception:
            pass

        elapsed   = int(time.time() - start)
        still_run = data.get("processing", False)
        new_text  = _build_progress_text(steps, elapsed, done=not still_run)

        # อัปเดต message เฉพาะเมื่อข้อความเปลี่ยน
        if new_text != last_text:
            try:
                bot.edit_message_text(new_text, chat_id, progress_msg_id)
                last_text = new_text
            except Exception:
                pass

        if still_run:
            continue

        # ── Pipeline เสร็จแล้ว ─────────────────────────────────────────────
        # ถ้ามี error steps → ส่ง error detail เป็น message แยก
        err_steps = [s for s in steps if s.get("status") == "error"]
        if err_steps:
            err_lines = ["⚠️ *พบ error ระหว่าง pipeline:*\n"]
            for s in err_steps:
                err_lines.append(f"❌ *{s.get('label', s.get('agent','?'))}*")
                if s.get("detail"):
                    err_lines.append(f"`{s['detail'][:300]}`")
            try:
                bot.send_message(chat_id, "\n".join(err_lines), parse_mode="Markdown")
            except Exception:
                bot.send_message(chat_id, "\n".join(err_lines))

        if data.get("error"):
            try:
                bot.send_message(
                    chat_id,
                    f"❌ *CaseFlow หยุดทำงาน:*\n`{data['error'][:400]}`",
                    parse_mode="Markdown",
                )
            except Exception:
                bot.send_message(chat_id, f"❌ CaseFlow error:\n{data['error'][:400]}")
            cf_state.pop(user_id, None)
            return

        if data.get("needs_branch_confirm"):
            pb    = data.get("pending_branch", "?")
            label = _BRANCH_LABELS.get(pb, pb)
            cf_state[user_id]["state"] = "waiting_confirm"
            bot.send_message(
                chat_id,
                f"🤔 *CaseFlow ตรวจจับว่าเป็น {label} (Branch {pb})*\n\n"
                f"พิมพ์ `ใช่` เพื่อยืนยัน หรือระบุ branch ที่ต้องการ (A/B/C/D/E/F/G/U)",
                parse_mode="Markdown",
            )
            return

        # Branch U — plan confirmation (second round)
        if data.get("needs_confirm"):
            cf_state[user_id]["state"] = "waiting_confirm"
            plan_text = data.get("draft", "(ไม่มีข้อมูล plan)")
            _cf_send(chat_id, f"📋 *แผนการทำงาน (Branch U)*\n\n{plan_text}")
            bot.send_message(
                chat_id,
                "พิมพ์ `ยืนยัน` เพื่อรัน หรือบอกสิ่งที่ต้องการเปลี่ยนแปลง",
                parse_mode="Markdown",
            )
            return

        draft = data.get("draft", "")
        if not draft:
            bot.send_message(chat_id, "⚠️ CaseFlow ไม่มีผลลัพธ์")
            cf_state.pop(user_id, None)
            return

        branch  = data.get("branch", "")
        version = data.get("version", 1)
        cf_state[user_id]["state"] = "done"

        header = f"📋 *Branch {branch} — {_BRANCH_LABELS.get(branch, '')} v{version}*\n\n"
        _cf_send(chat_id, header + draft)

        # QA review (ถ้ามี)
        qa = data.get("qa_review", "")
        if qa:
            _cf_send(chat_id, f"🔍 *QA Review:*\n\n{qa}")

        bot.send_message(
            chat_id,
            "✅ `/cfapprove` บันทึกลง vault | `/cfnew` เคสใหม่",
            parse_mode="Markdown",
        )


def _start_caseflow(user_id: str, chat_id: int, text: str):
    """POST /start → spawn polling thread."""
    try:
        r = _req.post(f"{CF_API}/start", json={"input": text}, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        bot.send_message(chat_id, f"❌ ติดต่อ CaseFlow ไม่ได้ (start\\_server.py รันอยู่ไหม?)\n`{e}`",
                         parse_mode="Markdown")
        return

    session_id = data["session_id"]
    cf_state[user_id] = {"session_id": session_id, "state": "processing"}
    progress_msg = bot.send_message(chat_id, "⏳ CaseFlow กำลังทำงาน...")
    threading.Thread(
        target=_poll_and_deliver,
        args=(user_id, chat_id, progress_msg.message_id),
        daemon=True,
    ).start()


def _feedback_caseflow(user_id: str, chat_id: int, text: str):
    """POST /feedback for revision/confirm, then spawn new polling thread."""
    info = cf_state.get(user_id)
    if not info:
        return
    session_id = info["session_id"]
    try:
        r = _req.post(f"{CF_API}/feedback",
                      json={"session_id": session_id, "message": text}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        bot.send_message(chat_id, f"❌ Feedback error: {e}")
        return

    cf_state[user_id]["state"] = "processing"
    progress_msg = bot.send_message(chat_id, "⏳ CaseFlow กำลังแก้ไข...")
    threading.Thread(
        target=_poll_and_deliver,
        args=(user_id, chat_id, progress_msg.message_id),
        daemon=True,
    ).start()


HELP_TEXT = """\
🤖 *CaseFlow Bot*

━━━ *CaseFlow* (วิเคราะห์เคส AI) ━━━
`/case <ข้อมูลผู้ป่วย>` — ส่งเคสเข้า CaseFlow
`/cf <ข้อมูลผู้ป่วย>` — เหมือนกัน (ชื่อสั้น)
`/cfnew` — เริ่มเคสใหม่ ล้าง session เดิม
`/cfapprove` — บันทึก report ลง vault
Branches: A=case | B=query | C=symptom | D=progress
          E=round | F=interpret | G=admission | U=freestyle

━━━ *Ward Buzzer* (สร้าง notes จากใบส่งเวร) ━━━
`/ward <ใบส่งเวร>` — สกัด diseases/drugs แล้ว confirm สร้าง notes
พิมพ์ `/confirm` เพื่อรัน vault builder

━━━ *Vault Manager* ━━━
`/mkdir <ชื่อ folder>` — สร้าง folder ใหม่ใน vault
`/mv <ไฟล์> | <folder>` — ย้ายไฟล์ (หา partial name ได้)
`/rename <ชื่อเก่า> | <ชื่อใหม่>` — เปลี่ยนชื่อ note
`/rm <ชื่อไฟล์>` — ลบ note (ต้อง confirm)
`/ls [folder]` — ดูโครงสร้าง หรือ list ไฟล์ใน folder

━━━ *Bot Commands* ━━━
`/help` — แสดงคำสั่งทั้งหมด
`/reset` — ล้างประวัติการคุย Gemini
`/search <คำ>` — ค้นหาใน vault
`/sync` — force sync GEMINI.md
`/gaps` — สแกน vault หา note ที่ขาด
`/confirm` — ยืนยัน action
`/cancel` — ยกเลิก pending action

━━━ *Vault Builder* ━━━
`/run <topic>` — สร้าง note หัวข้อเดียว
`/build <topic>` — เหมือนกัน

พิมพ์ข้อความธรรมดา → คุยกับ Gemini
"""

# ==========================================
# 2. CHAT STATE
# ==========================================
# pending confirmation: user_id → {"topics": {type: [names]}, "json_path": str}
pending_confirmation: dict[str, dict] = {}

# Gemini chat history: user_id → list of Content objects (SDK native)
_chat_history: dict[str, list] = {}

# ==========================================
# 3. VAULT SCANNER — auto-update GEMINI.md
# ==========================================
def scan_vault_folders() -> list[tuple[str, int]]:
    """Return list of (folder_name, note_count) sorted by name."""
    if not os.path.exists(VAULT_PATH):
        return []
    result = []
    for item in sorted(os.listdir(VAULT_PATH)):
        full = os.path.join(VAULT_PATH, item)
        if os.path.isdir(full) and not item.startswith("."):
            count = sum(1 for f in os.listdir(full) if f.endswith(".md"))
            result.append((item, count))
    return result


def update_vault_context() -> bool:
    """
    Rescan vault → update GEMINI.md vault structure section + vault_manifest.txt.
    Returns True if GEMINI.md was changed.
    """
    folders = scan_vault_folders()

    # Build structure block
    lines = ["```", "obsidian/"]
    for i, (folder, count) in enumerate(folders):
        prefix = "└──" if i == len(folders) - 1 else "├──"
        note_label = f"{count} note{'s' if count != 1 else ''}"
        lines.append(f"{prefix} {folder}/   ← {note_label}")
    lines.append("```")
    new_block = "\n".join(lines)

    gemini_md = os.path.join(CASEFLOW_PATH, "GEMINI.md")
    with open(gemini_md, "r", encoding="utf-8") as f:
        content = f.read()

    updated = re.sub(
        r"## Vault Folder Structure\n\n```.*?```",
        f"## Vault Folder Structure\n\n{new_block}",
        content,
        flags=re.DOTALL,
    )
    changed = updated != content
    if changed:
        with open(gemini_md, "w", encoding="utf-8") as f:
            f.write(updated)

    # Update vault_manifest.txt
    manifest_path = os.path.join(CASEFLOW_PATH, "vault_manifest.txt")
    files = []
    for root, dirs, filenames in os.walk(VAULT_PATH):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
        for fname in sorted(filenames):
            if fname.endswith(".md"):
                rel = os.path.relpath(os.path.join(root, fname), CASEFLOW_PATH)
                files.append(rel)
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write("\n".join(files))

    print(f"[vault] sync: {len(folders)} folders, {len(files)} notes"
          + (" — GEMINI.md updated" if changed else ""))
    return changed


# ==========================================
# 4. VAULT SEARCH (no AI, no tokens)
# ==========================================
def search_vault(query: str, max_results: int = 10) -> list[dict]:
    """Full-text search across all .md files in vault."""
    q = query.lower()
    results = []
    for root, dirs, files in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if q not in content.lower():
                    continue
                matches = [
                    line.strip() for line in content.splitlines()
                    if q in line.lower() and line.strip()
                ][:3]
                rel = os.path.relpath(fpath, VAULT_PATH)
                results.append({"file": rel, "matches": matches})
            except Exception:
                pass
            if len(results) >= max_results:
                return results
    return results


def format_search_results(query: str, results: list[dict]) -> str:
    if not results:
        return f"🔍 ไม่พบ `{query}` ใน vault"
    lines = [f"🔍 *{query}* — พบ {len(results)} ไฟล์\n"]
    for r in results:
        lines.append(f"📄 `{r['file']}`")
        for m in r["matches"]:
            lines.append(f"   › {m[:100]}")
    return "\n".join(lines)


def format_vault_structure(subfolder: str = "") -> str:
    if subfolder:
        # list files inside a specific folder
        target = os.path.join(VAULT_PATH, subfolder) if not os.path.isabs(subfolder) else subfolder
        if not os.path.isdir(target):
            # try partial match
            for item in os.listdir(VAULT_PATH):
                if subfolder.lower() in item.lower() and os.path.isdir(os.path.join(VAULT_PATH, item)):
                    target = os.path.join(VAULT_PATH, item)
                    subfolder = item
                    break
            else:
                return f"❌ ไม่พบ folder `{subfolder}`"
        files = [f for f in sorted(os.listdir(target)) if f.endswith(".md")]
        if not files:
            return f"📂 *{subfolder}* — ว่างเปล่า"
        lines = [f"📂 *{subfolder}* ({len(files)} notes)\n```"]
        for f in files:
            lines.append(f"  {f[:-3]}")
        lines.append("```")
        return "\n".join(lines)

    folders = scan_vault_folders()
    if not folders:
        return "❌ ไม่พบ vault ที่ `obsidian/`"
    lines = ["📂 *Vault Structure*\n```"]
    for i, (folder, count) in enumerate(folders):
        prefix = "└──" if i == len(folders) - 1 else "├──"
        lines.append(f"{prefix} {folder}/  ({count})")
    lines.append("```")
    return "\n".join(lines)


# ── Vault file management helpers ─────────────────────────────────────────────

def _vault_find_files(name: str) -> list[str]:
    """Find .md files whose stem contains `name` (case-insensitive)."""
    q = name.lower().replace(".md", "").strip()
    matches = []
    for root, dirs, files in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".md") and q in f[:-3].lower():
                matches.append(os.path.join(root, f))
    return matches


def _vault_find_folder(name: str) -> str | None:
    """Find vault folder whose name contains `name` (case-insensitive)."""
    q = name.lower().strip()
    for item in sorted(os.listdir(VAULT_PATH)):
        full = os.path.join(VAULT_PATH, item)
        if os.path.isdir(full) and q in item.lower():
            return full
    return None


# ==========================================
# 5. UTILITY FUNCTIONS
# ==========================================
def clean_ansi(text: str) -> str:
    return re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])').sub('', text)


def clean_output(text: str) -> str:
    patterns = [
        r"View Observations Live @.*?\n",
        r"# claude-mem status.*?\n",
        r"Ripgrep is not available.*?\n",
        r"Hook system message:.*?\n",
        r"Memory injection starts.*?\n",
        r"How it works:.*?\n",
        r"This message disappears.*?\n",
    ]
    for p in patterns:
        text = re.sub(p, "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


_TELEGRAM_FORMAT_INSTRUCTION = """
## Telegram Formatting Rules
Format ALL responses for Telegram using these rules:
- Section headers: use *Header Name* (single asterisk, NOT ##)
- Bold: *bold text* (single asterisk, NOT **)
- Italic: _italic_
- Inline code / drug names / lab values: `like this`
- Bullet lists: use • or - as prefix
- Tables: plain text or simple ASCII (Telegram tables don't render)
- NO markdown headers (##, ###) — they render as raw text
- NO horizontal rules (---)
- Keep responses concise and structured
"""

def _load_system_prompt() -> str:
    try:
        with open(os.path.join(CASEFLOW_PATH, "GEMINI.md"), encoding="utf-8") as f:
            base = f.read()
    except Exception:
        base = ""
    return base + _TELEGRAM_FORMAT_INSTRUCTION


def _fmt_telegram(text: str) -> str:
    """Convert standard markdown → Telegram Markdown v1."""
    # **bold** → *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # ### Header / ## Header / # Header → *Header*
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # Horizontal rules → blank line
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)
    # Collapse 3+ blank lines → 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _gemini_chat(user_id: str, parts) -> str:
    """Core chat call — parts can be str or list[Part]."""
    if not _GEMINI_OK:
        return "❌ Gemini API ไม่พร้อม — เช็ค GOOGLE_API_KEY ใน .env"
    history = _chat_history.get(user_id, [])
    try:
        from google.genai import types as _gt
        config = _gt.GenerateContentConfig(system_instruction=_load_system_prompt())
        chat = _gemini.chats.create(model=GEMINI_CHAT_MODEL, history=history, config=config)
        resp = chat.send_message(parts)
        _chat_history[user_id] = list(chat.get_history())
        return resp.text or "(ไม่มีข้อความตอบกลับ)"
    except Exception as e:
        err = str(e)
        if "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
            return "❌ Gemini API quota หมด — ลองใหม่ทีหลัง"
        if "INVALID_ARGUMENT" in err or "history" in err.lower():
            _chat_history.pop(user_id, None)
            return f"❌ Gemini error (history cleared, ลองอีกครั้ง): {err[:150]}"
        return f"❌ Gemini error: {err[:250]}"


def _call_gemini(user_id: str, text: str) -> str:
    return _gemini_chat(user_id, text)


def _tg_download(file_id: str) -> tuple[bytes, str]:
    """Download Telegram file → (bytes, mime_type)."""
    info = bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{API_TOKEN}/{info.file_path}"
    r = _req.get(url, timeout=60)
    r.raise_for_status()
    ext = info.file_path.rsplit(".", 1)[-1].lower()
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "heic": "image/heic",
        "pdf": "application/pdf",
        "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
        "mp3": "audio/mpeg", "mp4": "audio/mp4", "m4a": "audio/mp4",
        "wav": "audio/wav", "flac": "audio/flac",
    }
    return r.content, mime_map.get(ext, "application/octet-stream")


_CF_PATTERNS = [
    r'(?:ชาย|หญิง|male|female)\s*\d+\s*(?:ปี|y)',
    r'(?:^|\s)(?:cc|chief\s*complaint)\s*:',
    r'(?:admit(?:ted)?|dx|ddx|assessment|imp(?:ression)?)\s*:',
    r'(?:progress|morning|ward)\s*(?:note|round)',
    r'HN\s*:?\s*\d+',
    r'on\s+(?:day|d)\s*\d+',
    r'(?:vital|bp|temp|spo2|rr|hr)\s*:',
]


def _detect_intent(text: str):
    """
    Detect natural-language intent from any message.
    Returns (action, args) or None (→ fall through to Gemini chat).

    Actions: caseflow | ward | gaps | run | search | mv | mkdir | rename | rm | ls
    """
    t = text.strip()
    tl = t.lower()

    # ── Vault file ops ────────────────────────────────────────────────────────
    m = re.search(r'ย้าย\s+(.+?)\s+ไป(?:ไว้)?(?:ใน)?\s+(.+)', t)
    if m:
        return ("mv", [m.group(1).strip(), m.group(2).strip()])

    m = re.search(r'สร้าง\s+(?:folder|โฟลเดอร์)\s+(.+)', t, re.IGNORECASE)
    if m:
        return ("mkdir", [m.group(1).strip()])

    m = re.search(r'(?:เปลี่ยนชื่อ|rename)\s+(.+?)\s+(?:เป็น|to|→|->)\s+(.+)', t, re.IGNORECASE)
    if m:
        return ("rename", [m.group(1).strip(), m.group(2).strip()])

    m = re.search(r'^ลบ(?:\s+(?:note|ไฟล์))?\s+(.+)$', t)
    if m:
        return ("rm", [m.group(1).strip()])

    m = re.search(r'(?:ดู|list|แสดง)\s+(?:files?\s+in\s+|ไฟล์ใน\s+)?(?:folder\s+|โฟลเดอร์\s+)?(.+)', t, re.IGNORECASE)
    if m:
        return ("ls", [m.group(1).strip()])

    # ── Vault build (single or multi-topic) ──────────────────────────────────
    m = re.search(r'(?:สร้าง|build|เพิ่ม|make)\s+(?:note|หัวข้อ)(?:\s+(?:เรื่อง|ว่าด้วย|เกี่ยวกับ|for|about))?\s+(.+)',
                  t, re.IGNORECASE)
    if m:
        # pass raw string; _run_topics will split if comma-separated
        return ("run", [m.group(1).strip()])

    # ── Search ────────────────────────────────────────────────────────────────
    m = re.search(r'^(?:ค้นหา|search|หา(?:ข้อมูล|เรื่อง)?)\s+(.+)', t, re.IGNORECASE)
    if m:
        return ("search", [m.group(1).strip()])

    # ── Gap finder ────────────────────────────────────────────────────────────
    if re.search(r'(?:gap|หัวข้อ(?:ที่)?ขาด|note(?:ที่)?ขาด|missing\s*topic)', tl):
        return ("gaps", [])

    # ── Ward handover ─────────────────────────────────────────────────────────
    if re.search(r'(?:ส่งเวร|ใบส่งเวร|hand[\s-]?over)', tl):
        body = re.sub(r'.{0,20}(?:ส่งเวร|ใบส่งเวร|handover)[:\s]*', '', t, count=1, flags=re.IGNORECASE).strip()
        return ("ward", [body or t])

    # ── CaseFlow (patient data detected) ─────────────────────────────────────
    if any(re.search(p, tl) for p in _CF_PATTERNS) and len(t) > 40:
        return ("caseflow", [t])

    return None  # fall through to Gemini chat


def _split_topics(raw: str) -> list[str]:
    """Split comma/newline/semicolon-separated topic list → clean list."""
    parts = re.split(r'[,;\n]+', raw)
    return [p.strip() for p in parts if p.strip()]


# ── Vault Builder live-progress helpers ──────────────────────────────────────

_VB_STATUS_FILE = os.path.join(CASEFLOW_PATH, ".vb_status.json")
_CF_URL_FILE    = os.path.join(CASEFLOW_PATH, ".cf_url")
_vb_running     = False   # guard: only one vault_builder at a time


def _build_vb_live_text(header: str, recent: list[str], start_ts: float) -> str:
    """Compose live-progress Telegram message: progress bar + recent stdout lines."""
    elapsed = int(time.time() - start_ts)
    parts = [f"⏳ {header}"]

    if os.path.exists(_VB_STATUS_FILE):
        try:
            st    = json.load(open(_VB_STATUS_FILE, encoding="utf-8"))
            n     = st.get("current_num", 0)
            total = st.get("total", 0)
            cur   = st.get("current", "")
            errs  = st.get("errors", [])
            if total > 1:
                pct = int(n / total * 100)
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                parts.append(f"`[{bar}]` {pct}% ({n}/{total})")
            if cur:
                parts.append(f"หัวข้อ: *{cur}*")
            if errs:
                parts.append(f"⚠️ errors: {len(errs)}")
        except Exception:
            pass

    if recent:
        snippet = "\n".join(recent[-4:])
        parts.append(f"```\n{snippet[:300]}\n```")

    parts.append(f"⏱ {elapsed}s")
    return "\n".join(parts)


def _run_vault_background(cmd: list, chat_id: int, header_text: str, done_text: str):
    """Start vault_builder in a background thread; edit one Telegram message live."""
    global _vb_running
    if _vb_running:
        bot.send_message(chat_id, "⚠️ vault_builder กำลังรันอยู่แล้ว — รอให้เสร็จก่อน")
        return
    _vb_running = True

    try:
        os.remove(_VB_STATUS_FILE)
    except Exception:
        pass

    try:
        prog_msg = bot.send_message(
            chat_id,
            f"⏳ {header_text}\n`[░░░░░░░░░░]` กำลังเริ่ม...",
            parse_mode="Markdown",
        )
    except Exception:
        prog_msg = None

    def _worker():
        global _vb_running
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="ignore",
                bufsize=1, shell=True, cwd=CASEFLOW_PATH,
            )
            recent: list[str] = []
            last_edit = 0.0
            start_ts  = time.time()

            for raw in process.stdout:
                line = clean_ansi(raw).strip()
                if not line:
                    continue
                recent.append(line)
                if len(recent) > 8:
                    recent.pop(0)
                now = time.time()
                if now - last_edit >= 3.5 and prog_msg:
                    txt = _build_vb_live_text(header_text, recent, start_ts)
                    try:
                        bot.edit_message_text(
                            txt, chat_id, prog_msg.message_id, parse_mode="Markdown"
                        )
                        last_edit = now
                    except Exception:
                        pass

            process.wait()
            elapsed = int(time.time() - start_ts)
            update_vault_context()

            if process.returncode == 0:
                final = f"✅ {done_text} ({elapsed}s)"
            else:
                err_lines = "\n".join(f"`{l}`" for l in recent[-3:]) if recent else ""
                final = f"❌ vault_builder error (exit code {process.returncode})\n{err_lines}"

            if prog_msg:
                try:
                    bot.edit_message_text(
                        final, chat_id, prog_msg.message_id, parse_mode="Markdown"
                    )
                except Exception:
                    bot.send_message(chat_id, final, parse_mode="Markdown")
            else:
                bot.send_message(chat_id, final, parse_mode="Markdown")
        finally:
            _vb_running = False

    threading.Thread(target=_worker, daemon=True).start()


def _run_topics(raw: str, chat_id: int):
    """Build vault notes for one or more topics — non-blocking, live progress."""
    topics = _split_topics(raw)
    if not topics:
        bot.send_message(chat_id, "⚠️ ไม่พบชื่อหัวข้อ")
        return

    if len(topics) == 1:
        topic  = topics[0]
        cmd    = [r".venv\Scripts\python.exe", "vault_builder.py", "--topic", topic]
        header = f"สร้าง note: *{topic}*"
        done   = f"สร้าง note `{topic}` เสร็จแล้ว!"
    else:
        classified: dict[str, list[str]] = {}
        for t in topics:
            cat = classify_topic(t)
            classified.setdefault(cat, []).append(t)
        tmp_json = os.path.join(CASEFLOW_PATH, "_run_topics_tmp.json")
        with open(tmp_json, "w", encoding="utf-8") as f:
            json.dump(classified, f, ensure_ascii=False, indent=2)
        short  = ", ".join(topics[:4]) + (f" +{len(topics)-4} อื่น" if len(topics) > 4 else "")
        cmd    = [r".venv\Scripts\python.exe", "vault_builder.py", "--from-json", tmp_json]
        header = f"สร้าง *{len(topics)} notes*: {short}"
        done   = f"สร้างครบ {len(topics)} notes แล้ว!"

    _run_vault_background(cmd, chat_id, header, done)


def _exec_intent(action: str, args: list, message, user_id: str):
    """Route a detected intent to the right handler."""
    if action == "caseflow":
        _start_caseflow(user_id, message.chat.id, args[0])
        return
    if action == "run":
        _run_topics(args[0], message.chat.id)
        return
    dispatch = {
        "mv":     lambda: f"/mv {args[0]} | {args[1]}",
        "mkdir":  lambda: f"/mkdir {args[0]}",
        "rename": lambda: f"/rename {args[0]} | {args[1]}",
        "rm":     lambda: f"/rm {args[0]}",
        "ls":     lambda: f"/ls {args[0]}",
        "search": lambda: f"/search {args[0]}",
        "gaps":   lambda: "/gaps",
        "ward":   lambda: f"/ward {args[0]}",
    }
    fn = dispatch.get(action)
    if fn:
        message.text = fn()
        handle_message(message)


# ==========================================
# 6. MESSAGE HANDLER
# ==========================================

def _get_status_text() -> str:
    lines = ["🖥 *CaseFlow Status*\n"]

    # ── CaseFlow Server ────────────────────────────────────────────────────
    try:
        r = _req.get(f"{CF_API}/health", timeout=3)
        if r.ok:
            pub = open(_CF_URL_FILE).read().strip() if os.path.exists(_CF_URL_FILE) else ""
            lines.append("🟢 *CaseFlow Server* — running")
            if pub:
                lines.append(f"   └ `{pub}`")
            else:
                lines.append("   └ local only (no tunnel)")
        else:
            lines.append("🔴 *CaseFlow Server* — error")
    except Exception:
        lines.append("🔴 *CaseFlow Server* — stopped")

    # ── Vault Builder ──────────────────────────────────────────────────────
    try:
        import psutil
        vb_running = any(
            "vault_builder" in " ".join(p.cmdline())
            for p in psutil.process_iter(["cmdline"])
            if p.info["cmdline"]
        )
    except Exception:
        vb_running = os.path.exists(_VB_STATUS_FILE)

    if vb_running and os.path.exists(_VB_STATUS_FILE):
        try:
            st = json.load(open(_VB_STATUS_FILE, encoding="utf-8"))
            cur   = st.get("current", "?")
            ctype = st.get("current_type", "")
            n     = st.get("current_num", 0)
            total = st.get("total", 0)
            done  = st.get("done", [])
            errs  = st.get("errors", [])
            elapsed = int(time.time() - st.get("ts", time.time()))
            pct   = int(n / total * 100) if total else 0
            bar   = "█" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(f"\n⚙️ *Vault Builder* — กำลังรัน")
            lines.append(f"   └ `[{bar}]` {pct}%  ({n}/{total})")
            lines.append(f"   └ ตอนนี้: *{cur}* ({ctype})")
            lines.append(f"   └ elapsed: {elapsed}s")
            if done:
                lines.append(f"   └ เสร็จล่าสุด: {done[-1]}")
            if errs:
                lines.append(f"   └ ⚠️ errors: {len(errs)}")
        except Exception:
            lines.append("\n⚙️ *Vault Builder* — กำลังรัน (ไม่มีข้อมูล progress)")
    elif not vb_running:
        lines.append("\n⬜ *Vault Builder* — stopped")
        # แสดง error จาก run ล่าสุด (ถ้ามี)
        if os.path.exists(_VB_STATUS_FILE):
            try:
                st = json.load(open(_VB_STATUS_FILE, encoding="utf-8"))
                if st.get("errors"):
                    lines.append(f"   └ errors จาก run ล่าสุด: {', '.join(st['errors'][:3])}")
            except Exception:
                pass

    # ── Vault summary ──────────────────────────────────────────────────────
    folders = scan_vault_folders()
    total_notes = sum(c for _, c in folders)
    lines.append(f"\n📂 *Vault*: {len(folders)} folders, {total_notes} notes")

    return "\n".join(lines)


def _send_response(chat_id: int, text: str, progress_msg_id: int | None = None):
    """Delete progress spinner and send formatted chunks."""
    if progress_msg_id:
        try:
            bot.delete_message(chat_id, progress_msg_id)
        except Exception:
            pass
    text = _fmt_telegram(text)
    chunks = [text[i:i+4000] for i in range(0, max(len(text), 1), 4000)]
    for chunk in chunks:
        try:
            bot.send_message(chat_id, chunk, parse_mode="Markdown")
        except Exception:
            bot.send_message(chat_id, chunk)


@bot.message_handler(
    func=lambda m: m.from_user.id == ALLOWED_USER_ID,
    content_types=["photo", "document", "voice", "audio", "video"],
)
def handle_media(message):
    """Handle images, PDFs, voice notes → Gemini Vision/Audio."""
    user_id = str(message.from_user.id)
    caption = (message.caption or "").strip()
    progress_msg = bot.reply_to(message, "⏳ กำลังวิเคราะห์...")
    try:
        from google.genai import types as _gt
        ct = message.content_type

        if ct == "photo":
            file_id = message.photo[-1].file_id
            data, mime = _tg_download(file_id)
            prompt = caption or "วิเคราะห์รูปนี้ — ถ้าเป็น lab/EKG/film/X-ray ให้ interpret ด้วย"
        elif ct == "document":
            doc_mime = message.document.mime_type or ""
            if not any(t in doc_mime for t in ("image", "pdf", "audio")):
                bot.edit_message_text("❌ รองรับเฉพาะรูป, PDF, ไฟล์เสียง", message.chat.id, progress_msg.message_id)
                return
            data, mime = _tg_download(message.document.file_id)
            if "pdf" in doc_mime:
                mime = "application/pdf"
            prompt = caption or "สรุปเนื้อหาของไฟล์นี้"
        elif ct in ("voice", "audio"):
            file_id = message.voice.file_id if ct == "voice" else message.audio.file_id
            data, mime = _tg_download(file_id)
            prompt = caption or "ถอดความและสรุปสิ่งที่พูด"
        else:
            bot.edit_message_text("❌ ไม่รองรับไฟล์ประเภทนี้", message.chat.id, progress_msg.message_id)
            return

        parts = [_gt.Part.from_bytes(data=data, mime_type=mime), _gt.Part.from_text(prompt)]
        response_text = _gemini_chat(user_id, parts)
        _send_response(message.chat.id, response_text, progress_msg.message_id)
    except Exception as e:
        try:
            bot.edit_message_text(f"❌ Error: {e}", message.chat.id, progress_msg.message_id)
        except Exception:
            bot.send_message(message.chat.id, f"❌ Error: {e}")


@bot.message_handler(func=lambda m: m.from_user.id == ALLOWED_USER_ID)
def handle_message(message):
    user_id = str(message.from_user.id)
    query = message.text.strip()

    # ── CaseFlow commands ────────────────────────────────────────────────────
    case_match = re.match(r'^/(case|cf)\s+(.*)', query, re.IGNORECASE | re.DOTALL)
    if case_match:
        text = case_match.group(2).strip()
        if not text:
            bot.reply_to(message, "⚠️ ใส่ข้อมูลผู้ป่วยด้วย เช่น `/case ชาย 65 ปี เหนื่อยหอบ...`",
                         parse_mode="Markdown")
            return
        pending_confirmation.pop(user_id, None)  # ล้าง /gaps pending ถ้ามี
        _start_caseflow(user_id, message.chat.id, text)
        return

    if query.lower() in ["/cfnew", "/cf_new"]:
        cf_state.pop(user_id, None)
        bot.reply_to(message, "🗑️ ล้าง CaseFlow session แล้ว — ส่ง `/case` เพื่อเริ่มเคสใหม่",
                     parse_mode="Markdown")
        return

    if query.lower() == "/cfapprove":
        info = cf_state.get(user_id)
        if not info or info.get("state") != "done":
            bot.reply_to(message, "⚠️ ไม่มี report ที่พร้อม approve — ต้องมี session ที่เสร็จแล้วก่อน")
            return
        try:
            r = _req.post(f"{CF_API}/approve",
                          json={"session_id": info["session_id"]}, timeout=30)
            r.raise_for_status()
            result = r.json()
            saved = result.get("saved", "")
            bot.reply_to(message,
                         f"✅ บันทึกแล้ว!\n`{saved}`\n\nReport ถูก save ลง `reports/` และ Obsidian Vault inbox",
                         parse_mode="Markdown")
        except Exception as e:
            bot.reply_to(message, f"❌ Approve error: {e}")
        return

    # ── /ward — สกัด medical buzzwords จากใบส่งเวร ────────────────────────────
    ward_match = re.match(r'^/ward\s+(.*)', query, re.IGNORECASE | re.DOTALL)
    if ward_match:
        ward_text = ward_match.group(1).strip()
        if len(ward_text) < 30:
            bot.reply_to(message, "⚠️ ข้อความสั้นเกินไป — paste ใบส่งเวรหลัง `/ward`")
            return
        if not _GEMINI_OK:
            bot.reply_to(message, "❌ Gemini API ไม่พร้อม — เช็ค GOOGLE_API_KEY ใน .env")
            return

        scanning_msg = bot.reply_to(message, "🔍 กำลังสกัด medical terms...")
        _uid = user_id

        def _do_ward():
            all_topics = _extract_ward_buzzwords(ward_text)
            new_topics = _filter_new_topics(all_topics)
            try:
                bot.delete_message(message.chat.id, scanning_msg.message_id)
            except Exception:
                pass
            if not all_topics:
                bot.send_message(message.chat.id, "❌ สกัด terms ไม่ได้ — ลองใหม่อีกครั้ง")
                return
            already = len(all_topics) - len(new_topics)
            if not new_topics:
                bot.send_message(
                    message.chat.id,
                    f"✅ ครบแล้ว — ทุก topic ({len(all_topics)}) มี note ใน vault แล้ว",
                )
                return
            classified: dict[str, list[str]] = {}
            for t in new_topics:
                cat = classify_topic(t)
                classified.setdefault(cat, []).append(t)
            json_path = os.path.join(CASEFLOW_PATH, "ward_topics.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(classified, f, ensure_ascii=False, indent=2)
            pending_confirmation[_uid] = {"topics": classified, "json_path": json_path}
            icons = {"diseases": "🏥", "drugs": "💊", "approaches": "📋", "labs": "🧪"}
            lines = [
                f"🔍 สกัดได้ *{len(all_topics)} topics* จากใบส่งเวร",
                f"ใน vault แล้ว: {already}  |  ต้องสร้างใหม่: *{len(new_topics)}*\n",
            ]
            for cat in ["diseases", "drugs", "approaches", "labs"]:
                items = classified.get(cat, [])
                if not items:
                    continue
                lines.append(f"{icons.get(cat,'📄')} *{cat.capitalize()}* ({len(items)}):")
                for item in items[:20]:
                    lines.append(f"  • {item}")
                if len(items) > 20:
                    lines.append(f"  … และอีก {len(items)-20} topics")
                lines.append("")
            lines.append("พิมพ์ `/confirm` เพื่อสร้าง notes ทั้งหมด | `/cancel` เพื่อยกเลิก")
            full = "\n".join(lines)
            for i in range(0, len(full), 3800):
                try:
                    bot.send_message(message.chat.id, full[i:i+3800], parse_mode="Markdown")
                except Exception:
                    bot.send_message(message.chat.id, full[i:i+3800])

        threading.Thread(target=_do_ward, daemon=True).start()
        return

    # ── CaseFlow follow-up (only while waiting for user confirmation) ──────────
    _CONFIRM_WORDS = {"yes", "ใช่", "ยืนยัน", "รัน", "ok"}
    _CANCEL_WORDS  = {"cancel", "ยกเลิก"}
    if user_id in cf_state and not query.startswith("/"):
        state = cf_state[user_id].get("state")
        if state == "processing":
            bot.reply_to(message, "⏳ CaseFlow กำลังทำงานอยู่ รอสักครู่...")
            return
        elif state == "waiting_confirm":
            # Vault pending_confirmation takes priority
            is_confirm_cancel = query.lower() in (_CONFIRM_WORDS | _CANCEL_WORDS)
            if not (is_confirm_cancel and user_id in pending_confirmation):
                _feedback_caseflow(user_id, message.chat.id, query)
                return
        # state == "done" → fall through to Gemini chat (no feedback loop)

    # ── Bot-level commands (no subprocess) ──────────────────────────────────
    if query.lower() in ["/help", "help"]:
        bot.reply_to(message, HELP_TEXT, parse_mode="Markdown")
        return

    if query.lower() in ["/status", "status", "สถานะ"]:
        bot.reply_to(message, _get_status_text(), parse_mode="Markdown")
        return

    if query.lower() in ["/reset", "clear", "เริ่มใหม่"]:
        _chat_history.pop(user_id, None)
        bot.reply_to(message, "🧹 ล้างประวัติการคุยเรียบร้อย เริ่มต้นใหม่ครับ")
        return

    if query.lower() == "/ls":
        bot.reply_to(message, format_vault_structure(), parse_mode="Markdown")
        return

    if query.lower() == "/sync":
        changed = update_vault_context()
        msg = "✅ Sync เสร็จแล้ว — GEMINI.md อัปเดตแล้ว" if changed else "✅ Sync เสร็จแล้ว — ไม่มีการเปลี่ยนแปลง"
        bot.reply_to(message, msg)
        return

    if query.lower() == "/gaps":
        scanning_msg = bot.reply_to(message, "🔍 กำลังสแกน vault...")
        missing = find_missing_topics(VAULT_PATH)
        total = sum(len(v) for v in missing.values())
        bot.delete_message(message.chat.id, scanning_msg.message_id)
        if total == 0:
            bot.reply_to(message, "✅ ไม่พบหัวข้อที่ขาด — vault ครบถ้วนแล้ว")
            return
        # เก็บ pending state
        json_path = os.path.join(CASEFLOW_PATH, "gap_topics.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(missing, f, ensure_ascii=False, indent=2)
        pending_confirmation[user_id] = {"topics": missing, "json_path": json_path}
        # ส่ง summary ก่อน (ไม่เกิน 4096) แล้วค่อย chunk รายละเอียด
        icons = {"diseases": "🏥", "drugs": "💊", "approaches": "📋", "labs": "🧪"}
        summary_lines = [f"🔍 พบ *{total} หัวข้อ* ที่ยังไม่มี note:\n"]
        for t in ["diseases", "drugs", "approaches", "labs"]:
            items = missing.get(t, [])
            if items:
                summary_lines.append(f"{icons.get(t,'📄')} *{t.capitalize()}*: {len(items)} หัวข้อ")
        summary_lines.append(f"\nพิมพ์ `/confirm` เพื่อสร้างทั้งหมด | `/cancel` เพื่อยกเลิก")
        bot.reply_to(message, "\n".join(summary_lines), parse_mode="Markdown")
        # ส่งรายละเอียดเป็น chunks ทีหลัง
        full_report = format_gap_report(missing)
        for i in range(0, len(full_report), 3800):
            try:
                bot.send_message(message.chat.id, full_report[i:i+3800], parse_mode="Markdown")
            except Exception:
                bot.send_message(message.chat.id, full_report[i:i+3800])
        return

    if query.lower() in ["/confirm", "yes", "ใช่", "ยืนยัน", "รัน", "ok"]:
        if user_id not in pending_confirmation:
            bot.reply_to(message, "⚠️ ไม่มี action ที่รอยืนยัน — ลอง /gaps ก่อน")
            return
        pending = pending_confirmation.pop(user_id)

        # ── confirm /rm ───────────────────────────────────────────────────────
        if pending.get("action") == "rm":
            deleted = []
            for fpath in pending["files"]:
                try:
                    os.remove(fpath)
                    deleted.append(os.path.basename(fpath))
                except Exception as e:
                    bot.send_message(message.chat.id, f"❌ ลบ `{os.path.basename(fpath)}` ไม่ได้: {e}")
            update_vault_context()
            names = "\n".join(f"  • `{n}`" for n in deleted)
            bot.reply_to(message, f"🗑️ ลบแล้ว:\n{names}", parse_mode="Markdown")
            return

        total = sum(len(v) for v in pending["topics"].values())
        src   = "gaps" if "gap" in pending.get("json_path", "") else "ward"
        cmd   = [r".venv\Scripts\python.exe", "vault_builder.py", "--from-json", pending["json_path"]]
        _run_vault_background(
            cmd, message.chat.id,
            f"สร้าง *{total} notes* (จาก {src})",
            f"สร้าง notes เสร็จแล้ว! ({total} topics)",
        )
        return

    if query.lower() in ["/cancel", "cancel", "ยกเลิก"]:
        if user_id in pending_confirmation:
            pending_confirmation.pop(user_id)
            bot.reply_to(message, "❌ ยกเลิกแล้ว")
        else:
            bot.reply_to(message, "ไม่มี action ที่รอยืนยัน")
        return

    search_match = re.match(r'^/search\s+(.*)', query, re.IGNORECASE)
    if search_match:
        search_q = search_match.group(1).strip()
        results = search_vault(search_q)
        bot.reply_to(message, format_search_results(search_q, results), parse_mode="Markdown")
        return

    # ── /ls [folder] ─────────────────────────────────────────────────────────
    ls_match = re.match(r'^/ls(?:\s+(.+))?$', query, re.IGNORECASE)
    if ls_match:
        folder_arg = (ls_match.group(1) or "").strip()
        bot.reply_to(message, format_vault_structure(folder_arg), parse_mode="Markdown")
        return

    # ── /mkdir <folder name> ──────────────────────────────────────────────────
    mkdir_match = re.match(r'^/mkdir\s+(.+)', query, re.IGNORECASE)
    if mkdir_match:
        folder_name = mkdir_match.group(1).strip()
        target = os.path.join(VAULT_PATH, folder_name)
        if os.path.exists(target):
            bot.reply_to(message, f"⚠️ folder `{folder_name}` มีอยู่แล้ว")
        else:
            os.makedirs(target, exist_ok=True)
            update_vault_context()
            bot.reply_to(message, f"✅ สร้าง folder `{folder_name}` เรียบร้อย")
        return

    # ── /mv <file> | <folder> ─────────────────────────────────────────────────
    mv_match = re.match(r'^/mv\s+(.+?)\s*\|\s*(.+)', query, re.IGNORECASE)
    if mv_match:
        file_q, folder_q = mv_match.group(1).strip(), mv_match.group(2).strip()
        found = _vault_find_files(file_q)
        if not found:
            bot.reply_to(message, f"❌ ไม่พบไฟล์ที่มี `{file_q}` ใน vault")
            return
        if len(found) > 5:
            bot.reply_to(message, f"⚠️ พบ {len(found)} ไฟล์ — ระบุชื่อให้ชัดขึ้น")
            return
        dest_folder = _vault_find_folder(folder_q)
        if not dest_folder:
            bot.reply_to(message, f"❌ ไม่พบ folder `{folder_q}` — ใช้ /ls ดู folder ที่มี")
            return
        if len(found) > 1:
            names = "\n".join(f"  • `{os.path.basename(f)}`" for f in found)
            bot.reply_to(message,
                f"⚠️ พบหลายไฟล์:\n{names}\n\nระบุชื่อให้ตรงขึ้น",
                parse_mode="Markdown")
            return
        src = found[0]
        dst = os.path.join(dest_folder, os.path.basename(src))
        if os.path.exists(dst):
            bot.reply_to(message, f"⚠️ มีไฟล์ชื่อเดียวกันใน `{os.path.basename(dest_folder)}` อยู่แล้ว")
            return
        shutil.move(src, dst)
        update_vault_context()
        bot.reply_to(message,
            f"✅ ย้าย `{os.path.basename(src)}`\n→ `{os.path.basename(dest_folder)}/`",
            parse_mode="Markdown")
        return

    # ── /rename <old> | <new> ─────────────────────────────────────────────────
    rename_match = re.match(r'^/rename\s+(.+?)\s*\|\s*(.+)', query, re.IGNORECASE)
    if rename_match:
        old_q, new_name = rename_match.group(1).strip(), rename_match.group(2).strip()
        if not new_name.endswith(".md"):
            new_name += ".md"
        found = _vault_find_files(old_q)
        if not found:
            bot.reply_to(message, f"❌ ไม่พบไฟล์ที่มี `{old_q}`")
            return
        if len(found) > 1:
            names = "\n".join(f"  • `{os.path.basename(f)}`" for f in found)
            bot.reply_to(message, f"⚠️ พบหลายไฟล์:\n{names}", parse_mode="Markdown")
            return
        src = found[0]
        dst = os.path.join(os.path.dirname(src), new_name)
        if os.path.exists(dst):
            bot.reply_to(message, f"⚠️ มีไฟล์ `{new_name}` อยู่แล้ว")
            return
        os.rename(src, dst)
        update_vault_context()
        bot.reply_to(message,
            f"✅ เปลี่ยนชื่อ `{os.path.basename(src)}` → `{new_name}`",
            parse_mode="Markdown")
        return

    # ── /rm <file> — ต้อง confirm ─────────────────────────────────────────────
    rm_match = re.match(r'^/rm\s+(.+)', query, re.IGNORECASE)
    if rm_match:
        file_q = rm_match.group(1).strip()
        found = _vault_find_files(file_q)
        if not found:
            bot.reply_to(message, f"❌ ไม่พบไฟล์ที่มี `{file_q}`")
            return
        if len(found) > 3:
            bot.reply_to(message, f"⚠️ พบ {len(found)} ไฟล์ — ระบุชื่อให้ชัดขึ้น")
            return
        names_str = "\n".join(f"  • `{os.path.relpath(f, VAULT_PATH)}`" for f in found)
        pending_confirmation[user_id] = {"action": "rm", "files": found}
        bot.reply_to(message,
            f"⚠️ *ยืนยันลบ?*\n{names_str}\n\nพิมพ์ `/confirm` เพื่อลบถาวร | `/cancel` เพื่อยกเลิก",
            parse_mode="Markdown")
        return

    # ── /run /build → vault_builder (single or multi-topic) ─────────────────
    run_match = re.match(r'^/(run|build|สร้าง)\s+(.*)', query, re.IGNORECASE)
    if run_match:
        raw = run_match.group(2).strip()
        _run_topics(raw, message.chat.id)
        return

    # ── Natural language — detect intent for ALL non-slash messages ──────────
    if not query.startswith("/"):
        intent = _detect_intent(query)
        if intent:
            action, args = intent
            _exec_intent(action, args, message, user_id)
            return

    # ── Plain text / unknown slash → Gemini SDK ───────────────────────────
    progress_msg = bot.reply_to(message, "⏳ Gemini กำลังคิด...")
    try:
        response_text = _fmt_telegram(_call_gemini(user_id, query))
        try:
            bot.delete_message(message.chat.id, progress_msg.message_id)
        except Exception:
            pass
        chunks = [response_text[i:i+4000] for i in range(0, max(len(response_text), 1), 4000)]
        for chunk in chunks:
            try:
                bot.send_message(message.chat.id, chunk, parse_mode="Markdown")
            except Exception:
                bot.send_message(message.chat.id, chunk)
    except Exception as e:
        try:
            bot.edit_message_text(f"❌ Error: {e}", message.chat.id, progress_msg.message_id)
        except Exception:
            bot.send_message(message.chat.id, f"❌ Error: {e}")


# ==========================================
# 7. AUTO-SYNC WATCHDOG
# ==========================================
def _vault_watchdog(interval_seconds: int = 30):
    """
    Background thread: ตรวจสอบ vault ทุก N วินาที
    ถ้า mtime ของ obsidian/ เปลี่ยน → auto-sync GEMINI.md + manifest
    (ครอบคลุม: สร้าง/ลบ/แก้ไข note ใน Obsidian app โดยตรง)
    """
    last_mtime: dict[str, float] = {}

    def _snapshot() -> dict[str, float]:
        snap = {}
        if not os.path.exists(VAULT_PATH):
            return snap
        for root, dirs, files in os.walk(VAULT_PATH):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if fname.endswith(".md"):
                    fpath = os.path.join(root, fname)
                    try:
                        snap[fpath] = os.path.getmtime(fpath)
                    except OSError:
                        pass
        return snap

    last_mtime = _snapshot()

    while True:
        time.sleep(interval_seconds)
        try:
            current = _snapshot()
            if current != last_mtime:
                added   = len(current) - len(last_mtime)
                changed = sum(1 for k, v in current.items() if last_mtime.get(k) != v)
                update_vault_context()
                print(f"[watchdog] vault changed (+{added} files, {changed} modified) — synced")
                last_mtime = current
        except Exception as e:
            print(f"[watchdog] error: {e}")


# ==========================================
# 8. STARTUP
# ==========================================
print("CaseFlow Bot starting...")
update_vault_context()
print("✅ Vault context synced")

# Start auto-sync watchdog (daemon — dies with main process)
_watcher = threading.Thread(target=_vault_watchdog, args=(30,), daemon=True)
_watcher.start()
print("[watchdog] Vault watchdog started (30s interval)")

def _send_startup_url():
    """รอ server พร้อม แล้วส่ง public URL ให้ admin ทาง Telegram."""
    for _ in range(15):          # รอสูงสุด 15 × 2s = 30s
        time.sleep(2)
        try:
            info = _req.get(f"{CF_API}/info", timeout=3).json()
            url  = info.get("public_url") or ""
            ui   = info.get("ui") or ""
            if url:
                msg = (
                    f"🚀 *CaseFlow พร้อมใช้งาน*\n\n"
                    f"🌐 Public URL:\n`{url}`\n\n"
                    f"📱 Mobile UI:\n{ui}\n\n"
                    f"📖 API Docs:\n{url}/docs"
                )
            else:
                msg = "🚀 *CaseFlow พร้อมใช้งาน* (Local only — ไม่มี public URL)"
            bot.send_message(ALLOWED_USER_ID, msg, parse_mode="Markdown")
            return
        except Exception:
            pass
    # server ไม่ตอบหลัง 30s
    bot.send_message(ALLOWED_USER_ID, "⚠️ Bot เริ่มทำงานแล้ว แต่ติดต่อ CaseFlow server ไม่ได้")

threading.Thread(target=_send_startup_url, daemon=True).start()

print("Bot is running — send /help for commands")
while True:
    try:
        bot.polling(none_stop=True, skip_pending=True)
    except Exception as _poll_err:
        err_str = str(_poll_err)
        if "409" in err_str or "Conflict" in err_str:
            print(f"[bot] 409 conflict — รอ 35s แล้ว retry...")
            time.sleep(35)
        else:
            print(f"[bot] polling error: {_poll_err} — retry in 5s")
            time.sleep(5)
