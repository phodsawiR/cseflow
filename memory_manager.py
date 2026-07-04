"""
Claude Memory Manager — จัดการ memory files ใน ~/.claude/projects/.../memory/
"""

import os
import re
import sys
import subprocess
from pathlib import Path
from datetime import datetime

import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

MEMORY_DIR = Path(r"C:\Users\ASUS\.claude\projects\C--Users-ASUS-Documents-caseflow\memory")
INDEX_FILE = MEMORY_DIR / "MEMORY.md"

TYPES = ["user", "feedback", "project", "reference"]

# ─── helpers ──────────────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def _write(path: Path, content: str):
    path.write_text(content, encoding="utf-8")

def _parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm

def _list_memory_files() -> list[Path]:
    return sorted([f for f in MEMORY_DIR.glob("*.md") if f.name != "MEMORY.md"])

def _read_index() -> str:
    return _read(INDEX_FILE) if INDEX_FILE.exists() else "# Memory Index\n"

def _update_index(filename: str, title: str, hook: str, remove: bool = False):
    content = _read_index()
    lines = content.splitlines()
    entry = f"- [{title}]({filename}) — {hook}"
    link_pattern = f"]({filename})"

    new_lines = [l for l in lines if link_pattern not in l]

    if not remove:
        new_lines.append(entry)

    _write(INDEX_FILE, "\n".join(new_lines) + "\n")

# ─── commands ─────────────────────────────────────────────────────────────────

def cmd_list():
    files = _list_memory_files()
    if not files:
        print("ยังไม่มี memory ใดๆ")
        return
    print(f"\n{'#':<4} {'ชื่อ':<35} {'ประเภท':<12} {'คำอธิบาย'}")
    print("─" * 90)
    for i, f in enumerate(files, 1):
        fm = _parse_frontmatter(_read(f))
        name = fm.get("name", f.stem)
        mtype = fm.get("type", "?")
        desc = fm.get("description", "")[:50]
        print(f"{i:<4} {name:<35} {mtype:<12} {desc}")
    print()

def cmd_view(query: str):
    files = _list_memory_files()
    matched = [f for f in files if query.lower() in f.stem.lower() or
               query.lower() in _parse_frontmatter(_read(f)).get("name", "").lower()]
    if not matched:
        print(f"ไม่พบ memory ที่ตรงกับ '{query}'")
        return
    target = matched[0]
    print(f"\n{'═'*60}")
    print(f"  {target.name}")
    print(f"{'═'*60}\n")
    print(_read(target))

def cmd_add():
    print("\n─── เพิ่ม Memory ใหม่ ───")
    name = input("slug (kebab-case เช่น user-role): ").strip()
    if not name:
        print("ยกเลิก")
        return

    print(f"ประเภท: {', '.join(TYPES)}")
    mtype = input("type: ").strip()
    if mtype not in TYPES:
        print(f"ประเภทไม่ถูกต้อง ต้องเป็น: {', '.join(TYPES)}")
        return

    desc = input("description (1 บรรทัด): ").strip()

    print("เนื้อหา (พิมพ์ END แล้วกด Enter เมื่อเสร็จ):")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    body = "\n".join(lines)

    hook = input("hook สำหรับ MEMORY.md index (สั้นๆ): ").strip() or desc[:80]

    filename = f"{name}.md"
    filepath = MEMORY_DIR / filename
    content = f"""---
name: {name}
description: {desc}
metadata:
  type: {mtype}
---

{body}
"""
    _write(filepath, content)
    _update_index(filename, name, hook)
    print(f"\n✓ บันทึกแล้วที่ {filepath}")

def cmd_edit(query: str):
    files = _list_memory_files()
    matched = [f for f in files if query.lower() in f.stem.lower()]
    if not matched:
        print(f"ไม่พบ '{query}'")
        return
    target = matched[0]
    subprocess.run(["notepad", str(target)])
    print(f"เปิดแก้ไข {target.name} แล้ว")

def cmd_delete(query: str):
    files = _list_memory_files()
    matched = [f for f in files if query.lower() in f.stem.lower()]
    if not matched:
        print(f"ไม่พบ '{query}'")
        return
    target = matched[0]
    confirm = input(f"ลบ {target.name}? (y/n): ").strip().lower()
    if confirm == "y":
        _update_index(target.name, "", "", remove=True)
        target.unlink()
        print(f"✓ ลบ {target.name} แล้ว")

def cmd_search(query: str):
    files = _list_memory_files()
    print(f"\nค้นหา '{query}' ในทุก memory:\n")
    found = False
    for f in files:
        content = _read(f)
        if query.lower() in content.lower():
            fm = _parse_frontmatter(content)
            name = fm.get("name", f.stem)
            lines = [l for l in content.splitlines() if query.lower() in l.lower()]
            print(f"  [{name}]")
            for l in lines[:3]:
                print(f"    → {l.strip()}")
            print()
            found = True
    if not found:
        print("  ไม่พบ")

def cmd_index():
    print("\n─── MEMORY.md ───\n")
    print(_read_index())

# ─── main ─────────────────────────────────────────────────────────────────────

HELP = """
Claude Memory Manager
─────────────────────────────────────
  list              แสดง memory ทั้งหมด
  view <query>      ดูเนื้อหา memory
  add               เพิ่ม memory ใหม่
  edit <query>      เปิดแก้ไขใน Notepad
  delete <query>    ลบ memory
  search <query>    ค้นหาข้อความในทุก memory
  index             แสดง MEMORY.md index
  help              แสดง help นี้
"""

def main():
    args = sys.argv[1:]
    if not args or args[0] == "help":
        print(HELP)
    elif args[0] == "list":
        cmd_list()
    elif args[0] == "view" and len(args) > 1:
        cmd_view(" ".join(args[1:]))
    elif args[0] == "add":
        cmd_add()
    elif args[0] == "edit" and len(args) > 1:
        cmd_edit(" ".join(args[1:]))
    elif args[0] == "delete" and len(args) > 1:
        cmd_delete(" ".join(args[1:]))
    elif args[0] == "search" and len(args) > 1:
        cmd_search(" ".join(args[1:]))
    elif args[0] == "index":
        cmd_index()
    else:
        print(HELP)

if __name__ == "__main__":
    main()
