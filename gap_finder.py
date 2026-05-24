"""
gap_finder.py — ค้นหา [[wikilinks]] ที่ยังไม่มี note ใน vault
แล้วจัดกลุ่มตาม type เพื่อส่งให้ vault_builder.py
"""
import os
import re
from collections import defaultdict

VAULT_PATH = os.path.join(os.path.dirname(__file__), "obsidian")

# ─── Classification heuristics ───────────────────────────────────────────────

APPROACH_KEYWORDS = [
    "approach to", "approach -", "management of", "workup", "evaluation of",
]
LAB_KEYWORDS = [
    "interpretation", "pattern", "abg", "cbc", "lft", "coagulation",
    "electrolyte", "profile", "panel",
]
DRUG_KEYWORDS = [
    # suffixes
    "mab", "pril", "sartan", "olol", "statin", "mycin", "cillin", "floxacin",
    "zole", "pam", "lam", "pine", "tidine", "prazole", "zosin", "triptan",
    # common ward drugs (exact / partial)
    "furosemide", "heparin", "warfarin", "insulin", "alteplase", "morphine",
    "norepinephrine", "adrenaline", "epinephrine", "amiodarone", "adenosine",
    "nicardipine", "hydrocortisone", "dexamethasone", "salbutamol", "omeprazole",
    "octreotide", "diazepam", "lorazepam", "phenytoin", "vancomycin", "meropenem",
    "ceftriaxone", "piperacillin", "azithromycin", "levofloxacin", "enoxaparin",
    "calcium gluconate", "n-acetylcysteine", "naloxone", "flumazenil",
]

# ─── Filters ─────────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_BLOCK_RE = re.compile(r'\^[a-z0-9]+$')

def _is_noise(link: str) -> bool:
    """กรองลิงก์ที่ไม่ใช่หัวข้อ note จริงๆ"""
    if _DATE_RE.match(link):       return True   # date links
    if _BLOCK_RE.search(link):     return True   # block refs
    if link.startswith("http"):    return True
    if len(link) <= 1:             return True
    return False


def classify_topic(name: str) -> str:
    lower = name.lower()
    if any(kw in lower for kw in APPROACH_KEYWORDS):
        return "approaches"
    if any(kw in lower for kw in LAB_KEYWORDS):
        return "labs"
    if any(lower.endswith(kw) or kw in lower for kw in DRUG_KEYWORDS):
        return "drugs"
    return "diseases"


# ─── Core scanner ────────────────────────────────────────────────────────────

def find_missing_topics(vault_path: str = VAULT_PATH) -> dict[str, list[str]]:
    """
    สแกน vault ทั้งหมด → หา [[wikilinks]] ที่ยังไม่มีไฟล์
    Returns: {"diseases": [...], "drugs": [...], "approaches": [...], "labs": [...]}
    """
    existing: set[str] = set()
    all_links: set[str] = set()

    for root, dirs, files in os.walk(vault_path):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
        for fname in files:
            if not fname.endswith(".md"):
                continue
            # บันทึก note ที่มีอยู่แล้ว (normalize)
            note_name = fname[:-3].replace("_", " ").strip()
            existing.add(note_name.lower())

            # อ่านและสกัด wikilinks
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                # [[link]], [[link|alias]], [[link#heading]]
                links = re.findall(r'\[\[([^\]|\n#]+?)(?:[|#][^\]\n]*)?\]\]', content)
                for link in links:
                    link = link.strip()
                    if link and not _is_noise(link):
                        all_links.add(link)
            except Exception:
                pass

    # หา missing
    missing = [lnk for lnk in all_links if lnk.lower() not in existing]

    # จัดกลุ่ม
    classified: dict[str, list[str]] = defaultdict(list)
    for topic in sorted(missing, key=str.lower):
        classified[classify_topic(topic)].append(topic)

    return dict(classified)


def format_gap_report(missing: dict[str, list[str]]) -> str:
    """สร้างข้อความสรุปสำหรับส่งผ่าน Telegram"""
    total = sum(len(v) for v in missing.values())
    if total == 0:
        return "✅ ไม่พบหัวข้อที่ขาด — vault ครบถ้วนแล้ว"

    icons = {"diseases": "🏥", "drugs": "💊", "approaches": "📋", "labs": "🧪"}
    lines = [f"🔍 พบ *{total} หัวข้อ* ที่มี wikilink แต่ยังไม่มี note:\n"]

    for t in ["diseases", "drugs", "approaches", "labs"]:
        items = missing.get(t, [])
        if not items:
            continue
        lines.append(f"{icons.get(t, '📄')} *{t.capitalize()}* ({len(items)}):")
        for item in items:
            lines.append(f"  • {item}")
        lines.append("")

    lines.append("รัน vault builder สำหรับทุกหัวข้อเลยไหม?")
    lines.append("พิมพ์ `/confirm` เพื่อยืนยัน หรือ `/cancel` เพื่อยกเลิก")
    return "\n".join(lines)


# ─── CLI (standalone) ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json, sys

    vault = sys.argv[1] if len(sys.argv) > 1 else VAULT_PATH
    missing = find_missing_topics(vault)
    total = sum(len(v) for v in missing.values())

    print(f"Missing topics: {total}")
    print(json.dumps(missing, ensure_ascii=False, indent=2))
