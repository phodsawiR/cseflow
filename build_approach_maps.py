"""
build_approach_maps.py — Batch-generate clinical algorithm flowcharts for Approach notes
ใช้ approach_algorithm agent สร้าง decision-tree flowchart แล้วแทรกไว้ด้านบนของแต่ละ note

Usage:
    python build_approach_maps.py [--dry-run] [--only "Dyspnea,Chest Pain"] [--overwrite]
"""
import argparse
import re
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

APPROACHES_DIR = r"C:\Users\USER\Obsidian vault\08 - Approaches"

_ALGO_SECTION_HEADER = "## Clinical Algorithm\n\n"
_ALGO_SECTION_RE     = re.compile(
    r'## (?:🧠 Systematic Approach Mindmap|Clinical Algorithm)\n\n```mermaid.*?```\n*(?:---\n)?',
    re.DOTALL
)

_SKIP_FILENAMES = {"Approach_to_On_Call.md", "Approach_to_Ward_Call.md", "Origin_approach.md"}


# ─── Agent call ──────────────────────────────────────────────────────────────

def _call_algorithm_agent(chief_complaint: str, note_content: str) -> str:
    from router import call_agent, load_prompt

    # ดึงแค่ส่วน Definition & Classification (ไม่เกิน 80 บรรทัด)
    lines = note_content.splitlines()
    section_lines, in_section = [], False
    for line in lines:
        if re.match(r'^##\s*(Definition|Classification|DDx|Approach|System|Clinical)', line, re.IGNORECASE):
            in_section = True
        elif line.startswith("## ") and in_section:
            break
        if in_section:
            section_lines.append(line)
        if len(section_lines) > 80:
            break

    classification_text = "\n".join(section_lines) if section_lines else note_content[:3000]

    prompt = (
        f"Chief complaint: {chief_complaint}\n\n"
        f"Existing classification / DDx categories from note:\n"
        f"{classification_text}"
    )

    return call_agent(
        "approach_flowchart",
        system=load_prompt("approach_algorithm"),
        prompt=prompt
    ).strip()


# ─── Mermaid extraction ───────────────────────────────────────────────────────

_TEXT_FIXES = [
    # agent ใช้ text แทนสัญลักษณ์ → แปลงกลับ
    (r'\bge\b',  '≥'),
    (r'\bGe\b',  '≥'),
    (r'\bGE\b',  '≥'),
    (r'\ble\b',  '≤'),
    (r'\bLe\b',  '≤'),
    (r'\bLE\b',  '≤'),
    (r'\bgt\b',  '>'),
    (r'\bGT\b',  '>'),
    (r'\blt\b',  '<'),
    (r'\bLT\b',  '<'),
]

def _fix_symbols(code: str) -> str:
    """แปลง text fallback กลับเป็นสัญลักษณ์จริง"""
    for pattern, replacement in _TEXT_FIXES:
        code = re.sub(pattern, replacement, code)
    return code


def _extract_mermaid(agent_output: str) -> str:
    """ดึง mermaid block แรกจาก output"""
    match = re.search(r'```mermaid\n(.*?)```', agent_output, re.DOTALL)
    if match:
        code = _fix_symbols(match.group(1).rstrip())
        return f"```mermaid\n{code}\n```"
    # fallback: ถ้า agent ส่ง code โดยไม่มี fence
    if "graph TD" in agent_output or "graph LR" in agent_output:
        return f"```mermaid\n{_fix_symbols(agent_output.strip())}\n```"
    return ""


# ─── Content manipulation ─────────────────────────────────────────────────────

def _has_algorithm_section(content: str) -> bool:
    return bool(_ALGO_SECTION_RE.search(content))


def _remove_old_section(content: str) -> str:
    """ลบ section algorithm/mindmap เดิมออก (ถ้ามี)"""
    return _ALGO_SECTION_RE.sub("", content)


def _insert_algorithm(content: str, mermaid_block: str) -> str:
    """
    แทรก algorithm block หลัง Tags line และก่อน section แรก
    รองรับ 2 format:
      A: # Title → Tags → ## Section
      B: # Title → Tags → --- → ## Section
    """
    lines = content.splitlines(keepends=True)

    # หา Tags line
    tags_idx = next(
        (i for i, l in enumerate(lines) if l.strip().startswith("Tags:")),
        None
    )

    insert_idx = None
    if tags_idx is not None:
        for i in range(tags_idx + 1, len(lines)):
            s = lines[i].strip()
            if s.startswith("---") or s.startswith("##"):
                insert_idx = i
                break
    else:
        for i, line in enumerate(lines):
            if line.startswith("##"):
                insert_idx = i
                break

    if insert_idx is None:
        insert_idx = min(5, len(lines))

    block = f"\n{_ALGO_SECTION_HEADER}{mermaid_block}\n\n---\n"
    lines.insert(insert_idx, block)
    return "".join(lines)


# ─── Filename → chief complaint ───────────────────────────────────────────────

def _filename_to_complaint(filename: str) -> str:
    name = Path(filename).stem
    name = re.sub(r'[_-]approach$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'^Approach[_-]to[_-]', '', name, flags=re.IGNORECASE)
    return name.replace("_", " ").strip()


# ─── Main ─────────────────────────────────────────────────────────────────────

def process_all(dry_run: bool = False, only: list[str] | None = None, overwrite: bool = False) -> None:
    files = sorted(Path(APPROACHES_DIR).glob("*.md"))

    print(f"\n[build_approach_maps] {len(files)} files in {APPROACHES_DIR}")
    if dry_run:
        print("[build_approach_maps] DRY RUN\n")
    if overwrite:
        print("[build_approach_maps] OVERWRITE mode — จะแทนที่ section เดิม\n")

    skipped_exists, skipped_filter, processed, failed = [], [], [], []

    for filepath in files:
        filename = filepath.name

        if filename in _SKIP_FILENAMES:
            skipped_filter.append(filename)
            continue

        complaint = _filename_to_complaint(filename)

        if only and not any(f.lower() in complaint.lower() for f in only):
            skipped_filter.append(filename)
            continue

        content = filepath.read_text(encoding="utf-8")

        if _has_algorithm_section(content) and not overwrite:
            print(f"  [SKIP] already has algorithm: {filename}")
            skipped_exists.append(filename)
            continue

        print(f"  [RUN] {filename} -- [{complaint}]")

        try:
            agent_output  = _call_algorithm_agent(complaint, content)
            mermaid_block = _extract_mermaid(agent_output)

            if not mermaid_block:
                print(f"  [WARN] no flowchart returned -- skip {filename}")
                failed.append(filename)
                continue

            # ลบ section เดิม (ถ้ามี) แล้วแทรกใหม่
            clean_content = _remove_old_section(content)
            new_content   = _insert_algorithm(clean_content, mermaid_block)

            if not dry_run:
                filepath.write_text(new_content, encoding="utf-8")
                print(f"  [OK] {filename}")
            else:
                preview = "\n".join(mermaid_block.splitlines()[:14])
                print(f"  [PREVIEW]:\n{preview}\n  ...\n")

            processed.append(filename)
            time.sleep(1)

        except Exception as e:
            print(f"  [ERROR] {filename} -- {e}")
            failed.append(filename)

    print(f"\n{'='*50}")
    print(f"Processed:        {len(processed)}")
    print(f"Skipped (exists): {len(skipped_exists)}")
    print(f"Skipped (filter): {len(skipped_filter)}")
    print(f"Failed:           {len(failed)}")
    if failed:
        print(f"  -> {', '.join(failed)}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch-generate clinical algorithm flowcharts")
    parser.add_argument("--dry-run",   action="store_true", help="Preview only, don't write files")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing algorithm sections")
    parser.add_argument("--only",      type=str, default="",
                        help="Comma-separated list to process, e.g. 'Dyspnea,Chest Pain'")
    args = parser.parse_args()

    only_list = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
    process_all(dry_run=args.dry_run, only=only_list, overwrite=args.overwrite)
