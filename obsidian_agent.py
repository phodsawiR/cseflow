import telebot
import subprocess
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

def _check_single_instance():
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            import psutil
            if psutil.pid_exists(old_pid):
                print(f"[obsidian_agent] already running (PID {old_pid}). Exiting.")
                sys.exit(0)
        except Exception:
            pass  # stale PID file — overwrite it
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(_PID_FILE) and os.remove(_PID_FILE))

_check_single_instance()
# ─────────────────────────────────────────────────────────────────────────────

# ==========================================
# 1. CONFIGURATION
# ==========================================
API_TOKEN = '***REDACTED***'
ALLOWED_USER_ID = ***REDACTED***
GEMINI_PATH = r"C:\Users\USER\AppData\Roaming\npm\gemini.cmd"
CASEFLOW_PATH = r"C:\Users\USER\OneDrive\Desktop\caseflow"
VAULT_PATH = os.path.join(CASEFLOW_PATH, "obsidian")
SESSIONS_FILE = os.path.join(CASEFLOW_PATH, "bot_sessions.json")

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
            "💬 ส่งข้อความต่อเพื่อแก้ไข | `/cfapprove` บันทึกลง vault | `/cfnew` เคสใหม่",
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


# Gemini CLI system commands (pass directly without prompt wrapping)
system_commands = {"/tools", "/model", "/stats", "/version"}

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

━━━ *Bot Commands* ━━━
`/help` — แสดงคำสั่งทั้งหมด
`/reset` — ล้าง Gemini session
`/search <คำ>` — ค้นหาใน vault
`/ls` — ดูโครงสร้าง vault
`/sync` — force sync GEMINI.md
`/gaps` — สแกน vault หา note ที่ขาด
`/confirm` — ยืนยันรัน vault builder
`/cancel` — ยกเลิก pending action

━━━ *Vault Builder* ━━━
`/run <topic>` — สร้าง note หัวข้อเดียว
`/build <topic>` — เหมือนกัน

━━━ *Gemini CLI* ━━━
`/tools` `/model` `/stats` `/version`
พิมพ์ข้อความธรรมดา → Gemini CLI
"""

# เรทราคาปี 2026 (USD per 1M tokens)
PRICING_TABLE = {
    "gemini-3.5-flash":      {"in": 0.08,  "out": 0.35},
    "gemini-3.1-pro-preview":{"in": 2.50,  "out": 10.00},
    "gemini-3.1-flash":      {"in": 0.10,  "out": 0.40},
    "gemini-1.5-flash":      {"in": 0.075, "out": 0.30},
}
EXCHANGE_RATE = 36.8

# ==========================================
# 2. SESSION PERSISTENCE
# ==========================================
def load_sessions() -> dict:
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_sessions(sessions: dict) -> None:
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)

active_sessions = load_sessions()

# pending confirmation: user_id → {"topics": {type: [names]}, "json_path": str}
pending_confirmation: dict[str, dict] = {}

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


def format_vault_structure() -> str:
    folders = scan_vault_folders()
    if not folders:
        return "❌ ไม่พบ vault ที่ `obsidian/`"
    lines = ["📂 *Vault Structure*\n```"]
    for i, (folder, count) in enumerate(folders):
        prefix = "└──" if i == len(folders) - 1 else "├──"
        lines.append(f"{prefix} {folder}/  ({count})")
    lines.append("```")
    return "\n".join(lines)


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


def calculate_dynamic_cost(stats: dict) -> tuple[float, list[str]]:
    total_thb = 0.0
    model_details = []
    for m_name, m_data in stats.get("models", {}).items():
        tokens = m_data.get("tokens", {})
        in_t  = tokens.get("input", 0)
        out_t = tokens.get("candidates", 0)
        rate = PRICING_TABLE.get("gemini-1.5-flash")
        for key in PRICING_TABLE:
            if key in m_name.lower():
                rate = PRICING_TABLE[key]
                break
        cost_usd = (in_t / 1_000_000) * rate["in"] + (out_t / 1_000_000) * rate["out"]
        total_thb += cost_usd * EXCHANGE_RATE
        short = m_name.split("/")[-1] if "/" in m_name else m_name
        model_details.append(f"   └ {short}: {in_t:,}/{out_t:,} t")
    return total_thb, model_details


# ==========================================
# 6. MESSAGE HANDLER
# ==========================================
@bot.message_handler(func=lambda m: m.from_user.id == ALLOWED_USER_ID)
def handle_gemini(message):
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

    # ── CaseFlow follow-up (revision / confirm) ──────────────────────────────
    if user_id in cf_state and not query.startswith("/"):
        state = cf_state[user_id].get("state")
        if state in ("done", "waiting_confirm"):
            _feedback_caseflow(user_id, message.chat.id, query)
            return
        elif state == "processing":
            bot.reply_to(message, "⏳ CaseFlow กำลังทำงานอยู่ รอสักครู่...")
            return

    # ── Bot-level commands (no subprocess) ──────────────────────────────────
    if query.lower() in ["/help", "help"]:
        bot.reply_to(message, HELP_TEXT, parse_mode="Markdown")
        return

    if query.lower() in ["/reset", "clear", "เริ่มใหม่"]:
        active_sessions.pop(user_id, None)
        save_sessions(active_sessions)
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
        total = sum(len(v) for v in pending["topics"].values())
        bot.reply_to(message, f"⚡ เริ่มสร้าง {total} notes — อาจใช้เวลานาน...")
        cmd = ["python", "vault_builder.py", "--from-json", pending["json_path"]]
        # รันใน subprocess ปกติ (จะ stream output กลับมา)
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="ignore",
                bufsize=1, shell=True, cwd=CASEFLOW_PATH,
            )
            last_update = 0
            full_out = ""
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    clean = clean_ansi(line).strip()
                    if clean:
                        full_out += line
                        if time.time() - last_update > 4.0:
                            try:
                                bot.send_message(message.chat.id, f"⏳ `{clean[:120]}`",
                                                 parse_mode="Markdown")
                                last_update = time.time()
                            except Exception:
                                pass
            process.wait()
            update_vault_context()
            bot.send_message(message.chat.id, f"✅ สร้าง notes เสร็จแล้ว! ({total} topics)")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Error: {e}")
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

    # ── Progress message ─────────────────────────────────────────────────────
    progress_msg = bot.reply_to(message, "⏳ Gemini กำลังทำงาน...")

    # ── Session management ───────────────────────────────────────────────────
    if user_id not in active_sessions:
        active_sessions[user_id] = {"id": f"user-{user_id}-{int(time.time())}", "is_new": True}
    session_info = active_sessions[user_id]

    # ── Command routing ──────────────────────────────────────────────────────
    run_match = re.match(r'^/(run|build|สร้าง)\s+(.*)', query, re.IGNORECASE)

    if run_match:
        topic = run_match.group(2).strip()
        cmd = ["python", "vault_builder.py", "--topic", topic]
        bot.reply_to(message, f"⚡ กำลังสร้าง note: `{topic}`...")
    elif query in system_commands:
        cmd = [GEMINI_PATH, query]
    else:
        cmd = [
            GEMINI_PATH,
            "--prompt", query,
            "--yolo",
            "--output-format", "json",
            "--model", "gemini-3.1-pro-preview",
        ]
        if session_info["is_new"]:
            cmd.extend(["--session-id", session_info["id"]])
            session_info["is_new"] = False
        else:
            cmd.extend(["--resume", session_info["id"]])

    # ── Subprocess ───────────────────────────────────────────────────────────
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            bufsize=1,
            shell=True,
            cwd=CASEFLOW_PATH,
        )

        full_raw_output = ""
        last_update_time = 0

        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                clean_line = clean_ansi(line).strip()
                if clean_line:
                    if not (clean_line.startswith("{") or clean_line.startswith("[")):
                        print(f"[gemini] {clean_line}")
                    full_raw_output += line
                    if time.time() - last_update_time > 3.0:
                        try:
                            bot.edit_message_text(
                                f"⏳ *กำลังประมวลผล...*\n`{clean_line[:100]}`",
                                message.chat.id, progress_msg.message_id,
                                parse_mode="Markdown",
                            )
                            last_update_time = time.time()
                        except Exception:
                            pass

        process.wait()
        bot.delete_message(message.chat.id, progress_msg.message_id)

        # Parse JSON output
        json_match = re.search(r'(\{.*\})', full_raw_output, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                main_response = data.get("response", "(ไม่มีข้อความตอบกลับ)")
                total_cost, details = calculate_dynamic_cost(data.get("stats", {}))
                usage = (
                    "\n\n📊 *Usage:*\n" + "\n".join(details) +
                    f"\n💰 {total_cost:.4f} THB"
                )
                final_text = main_response + usage
            except Exception:
                final_text = clean_ansi(full_raw_output)
        else:
            final_text = clean_ansi(full_raw_output)

        final_text = clean_output(final_text)
        if len(final_text) > 4000:
            for i in range(0, len(final_text), 4000):
                bot.send_message(message.chat.id, final_text[i:i+4000])
        else:
            bot.send_message(message.chat.id, final_text)

        # Auto-sync vault context หลังทุก subprocess call
        update_vault_context()
        save_sessions(active_sessions)

    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {str(e)}")


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
print("🤖 CaseFlow Bot starting...")
update_vault_context()
print("✅ Vault context synced")

# Start auto-sync watchdog (daemon — dies with main process)
_watcher = threading.Thread(target=_vault_watchdog, args=(30,), daemon=True)
_watcher.start()
print("👁️  Vault watchdog started (30s interval)")

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

print("🚀 Bot is running — send /help for commands")
bot.polling(none_stop=True, skip_pending=True)
