import telebot
import subprocess
import time
import re
import os
import json
import threading
from gap_finder import find_missing_topics, format_gap_report

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

# Gemini CLI system commands (pass directly without prompt wrapping)
system_commands = {"/tools", "/model", "/stats", "/version"}

HELP_TEXT = """\
🤖 *CaseFlow Bot* — Gemini CLI via Telegram

━━━ *Bot Commands* ━━━
`/help` — แสดงคำสั่งทั้งหมด
`/reset` — ล้าง session / เริ่มการสนทนาใหม่
`/search <คำ>` — ค้นหาเนื้อหาในทุก note ของ vault
`/ls` — ดูโครงสร้าง vault ปัจจุบัน
`/sync` — force sync GEMINI.md ทันที (ปกติ auto ทุก 30 วิ)
`/gaps` — สแกน vault หา wikilinks ที่ยังไม่มี note
`/confirm` — ยืนยันรัน vault builder จาก /gaps
`/cancel` — ยกเลิก pending action

━━━ *Vault Builder* (สร้าง Note ด้วย AI) ━━━
`/run <topic>` — สร้าง note หัวข้อเดียว
`/build <topic>` — เหมือนกัน
`/สร้าง <topic>` — เหมือนกัน (ภาษาไทย)

ตัวอย่าง: `/run Sepsis` หรือ `/build Heart Failure`

━━━ *Gemini CLI System Commands* ━━━
`/tools` — ดู tools ที่ Gemini มี
`/model` — ดู/เปลี่ยน model ที่ใช้
`/stats` — ดู token usage สะสม
`/version` — ดู Gemini CLI version

━━━ *Natural Language* (พิมพ์เลย ไม่ต้อง /) ━━━
• สร้าง folder Physical Examination ใน vault
• ดูไฟล์ทั้งหมดใน 02 - Diseases
• แก้ไข Heart Failure.md เพิ่มหัวข้อ Management
• รัน vault\\_linker.py เพื่อเพิ่ม wikilinks
• ย้าย Sepsis.md ไปไว้ใน 02 - Diseases
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
        bot.reply_to(message, format_gap_report(missing), parse_mode="Markdown")
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

print("🚀 Bot is running — send /help for commands")
bot.polling(none_stop=True, skip_pending=True)
