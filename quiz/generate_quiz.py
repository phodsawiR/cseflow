"""
Quiz Generator — Exam KB + Obsidian vault → Ollama → Firebase

Modes:
  --mode random       สุ่มโรคจาก exam KB
  --mode topic        กำหนดเรื่อง (ต้องใช้ --topic)
  --mode kb-map       ครอบคลุมทุกโรคใน exam KB disease_index (เรียงตาม frequency)
  --mode vault-map    ดึงชื่อ note จาก Obsidian vault มาเป็น topic list
  --mode scope        parse scope note (G1 output) → ใช้ชื่อโรค + แนวที่ถาม เป็น context

Examples:
  python quiz/generate_quiz.py --mode scope                        # auto-find scope note
  python quiz/generate_quiz.py --mode scope --tier must_know --n 5
  python quiz/generate_quiz.py --mode topic --topic "HIV" --n 5
  python quiz/generate_quiz.py --mode kb-map --n 2
  python quiz/generate_quiz.py --mode random --n 10
"""
import argparse
import hashlib
import json
import os
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import ollama
import requests
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
FIREBASE_PROJECT = os.getenv("FIREBASE_PROJECT_ID", "medguide-34566")
OLLAMA_MODEL     = os.getenv("OLLAMA_QUIZ_MODEL", "qwen2.5:14b")
OLLAMA_THREADS   = int(os.getenv("OLLAMA_NUM_THREADS") or os.cpu_count() or 16)
EXAM_KB_PATH     = os.getenv("EXAM_KB_PATH", "./examflow/exam_kb.json")
OBSIDIAN_PATH    = Path(os.getenv("OBSIDIAN_VAULT_PATH", r"C:\Users\ASUS\Documents\Obsidian vault"))
QUIZ_LOG_PATH    = Path("quiz/quiz_log.json")   # dedup log


# ── Dedup log ─────────────────────────────────────────────────────────────────

def _load_log() -> set[str]:
    if QUIZ_LOG_PATH.exists():
        data = json.loads(QUIZ_LOG_PATH.read_text(encoding="utf-8"))
        return set(data.get("fingerprints", []))
    return set()


def _save_log(fps: set[str]):
    QUIZ_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUIZ_LOG_PATH.write_text(
        json.dumps({"fingerprints": sorted(fps), "updated": datetime.now().isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def _fingerprint(question_text: str) -> str:
    norm = re.sub(r"[\s\W]+", "", question_text.lower())[:150]
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


# ── Firebase ──────────────────────────────────────────────────────────────────

def _firebase_token() -> str:
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}"
    r = requests.post(url, json={"returnSecureToken": True}, timeout=10)
    r.raise_for_status()
    return r.json()["idToken"]


def _push_firestore(doc: dict, token: str) -> str:
    url = (f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
           f"/databases/(default)/documents/quizzes")
    fields = {}
    for k, v in doc.items():
        if isinstance(v, str):   fields[k] = {"stringValue": v}
        elif isinstance(v, int): fields[k] = {"integerValue": str(v)}
        elif isinstance(v, list):
            fields[k] = {"arrayValue": {"values": [{"stringValue": str(i)} for i in v]}}
    r = requests.post(url, json={"fields": fields},
                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return r.json().get("name", "").split("/")[-1]


# ── Topic sources ─────────────────────────────────────────────────────────────

def _topics_from_kb(kb: dict, mode: str = "all") -> list[tuple[str, str | None]]:
    """Returns list of (topic, system) tuples from exam KB disease_index."""
    disease_idx = kb.get("disease_index", {})
    if not disease_idx:
        return []

    entries = [(name, entry) for name, entry in disease_idx.items()
               if entry.get("frequency", 0) > 0]

    if mode == "high-yield":
        # Only diseases appearing 2+ times
        entries = [(n, e) for n, e in entries if e.get("frequency", 0) >= 2]

    # Sort by frequency descending
    entries.sort(key=lambda x: x[1].get("frequency", 0), reverse=True)

    # Map disease → system via system_index
    disease_to_system: dict[str, str] = {}
    for sys_name, sys_entry in kb.get("system_index", {}).items():
        for d in sys_entry.get("diseases", []):
            if d not in disease_to_system:
                disease_to_system[d] = sys_name

    return [(name, disease_to_system.get(name)) for name, _ in entries]


def _find_scope_notes() -> list[Path]:
    """Find all scope notes in vault (files with 'scope' in name)."""
    return sorted(OBSIDIAN_PATH.rglob("scope*.md"))


def _parse_scope_note(path: Path) -> list[dict]:
    """
    Parse G1 scope note → list of entries with:
      disease, system, frequency, tier (must_know/high_yield/nice_to_know), key_topics
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    entries = []

    # ── Must Know table ───────────────────────────────────────────────────────
    # | **Disease** | System | count | แนวที่ถาม |
    table_re = re.compile(
        r"\|\s*\*\*(.+?)\*\*\s*\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|"
    )
    for m in table_re.finditer(text):
        disease   = m.group(1).strip()
        system    = m.group(2).strip()
        frequency = int(m.group(3))
        key_topics = re.sub(r"\s+", " ", m.group(4).strip())
        entries.append({
            "disease":    disease,
            "system":     system,
            "frequency":  frequency,
            "tier":       "must_know",
            "key_topics": key_topics,
        })

    # ── High Yield & Nice to Know bullet lists ────────────────────────────────
    # - **Disease** — description
    current_tier = None
    for line in text.splitlines():
        if "High Yield" in line:
            current_tier = "high_yield"
        elif "Nice to Know" in line:
            current_tier = "nice_to_know"

        if current_tier and line.strip().startswith("- **"):
            bullet_re = re.match(r"-\s*\*\*(.+?)\*\*\s*(?:—\s*(.+))?", line.strip())
            if bullet_re:
                disease    = bullet_re.group(1).strip()
                key_topics = (bullet_re.group(2) or "").strip()
                # Skip if already in must_know
                if not any(e["disease"] == disease for e in entries):
                    entries.append({
                        "disease":    disease,
                        "system":     None,
                        "frequency":  2 if current_tier == "high_yield" else 1,
                        "tier":       current_tier,
                        "key_topics": key_topics,
                    })

    return entries


def _topics_from_scope(tier_filter: str | None = None) -> list[dict]:
    """Find scope notes and parse them. Returns entries sorted by frequency."""
    notes = _find_scope_notes()
    if not notes:
        print(f"[quiz] No scope notes found in vault (looking for scope*.md)")
        return []

    print(f"[quiz] Found scope notes: {[n.name for n in notes]}")
    all_entries: list[dict] = []
    for note in notes:
        entries = _parse_scope_note(note)
        print(f"  {note.name}: {len(entries)} diseases")
        all_entries.extend(entries)

    # Dedup by disease name (keep highest frequency)
    seen: dict[str, dict] = {}
    for e in all_entries:
        name = e["disease"].lower()
        if name not in seen or e["frequency"] > seen[name]["frequency"]:
            seen[name] = e

    result = list(seen.values())
    result.sort(key=lambda x: x["frequency"], reverse=True)

    # Filter by tier
    tier_map = {"must_know": "must_know", "high_yield": "high_yield",
                "nice_to_know": "nice_to_know", "must": "must_know", "high": "high_yield"}
    if tier_filter and tier_filter in tier_map:
        result = [e for e in result if e["tier"] == tier_map[tier_filter]]

    return result


def _topics_from_vault(folder: str | None = None) -> list[tuple[str, str | None]]:
    """Returns (topic, folder_name) from Obsidian vault .md filenames."""
    search_root = OBSIDIAN_PATH / folder if folder else OBSIDIAN_PATH
    if not search_root.exists():
        print(f"[quiz] Vault path not found: {search_root}")
        return []

    topics = []
    for md in sorted(search_root.rglob("*.md")):
        name = md.stem
        # Skip system/index files
        if name.startswith("_") or name.lower() in ("readme", "index", "home"):
            continue
        # Use parent folder as system hint
        sys_hint = md.parent.name if md.parent != OBSIDIAN_PATH else None
        topics.append((name, sys_hint))

    return topics


# ── Context builders ──────────────────────────────────────────────────────────

def _kb_context(kb: dict, topic: str, system: str | None, max_q: int = 6) -> str:
    topic_lower = topic.lower()
    matched = [
        q for q in kb["questions"]
        if topic_lower in " ".join(q.get("diseases", [])).lower()
        or topic_lower in " ".join(q.get("systems", [])).lower()
        or topic_lower in q.get("question_text", "").lower()
    ]
    if not matched and system:
        matched = [q for q in kb["questions"]
                   if system.lower() in " ".join(q.get("systems", [])).lower()]
    if not matched:
        matched = random.sample(kb["questions"], min(4, len(kb["questions"])))

    sample = random.sample(matched, min(max_q, len(matched)))
    lines = ["=== Exam KB Pattern Reference ==="]
    for q in sample:
        lines.append(f"\nQ: {q['question_text']}")
        for c in q.get("choices", []):
            lines.append(f"  {c}")
        if q.get("answer_key"):
            lines.append(f"  Answer: {q['answer_key']}")
        if q.get("distractors", {}).get("trap"):
            lines.append(f"  Pitfall: {q['distractors']['trap']}")
    return "\n".join(lines)


def _vault_context(topic: str, max_chars: int = 3000) -> str:
    topic_lower = topic.lower()
    found: list[tuple[int, Path]] = []

    for md in OBSIDIAN_PATH.rglob("*.md"):
        if topic_lower in md.stem.lower():
            found.append((0, md))
        else:
            try:
                if topic_lower in md.read_text(encoding="utf-8", errors="ignore").lower():
                    found.append((1, md))
            except Exception:
                pass

    if not found:
        return ""

    found.sort(key=lambda x: x[0])
    lines = ["=== Obsidian Vault Notes ==="]
    total = 0
    for _, f in found[:3]:
        snippet = f.read_text(encoding="utf-8", errors="ignore")[: max_chars // 3]
        lines.append(f"\n--- {f.stem} ---\n{snippet}")
        total += len(snippet)
        if total >= max_chars:
            break
    return "\n".join(lines)


# ── Generation ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a Thai medical exam question writer for Year 4 medical students.
Generate a single high-quality MCQ question based on the provided context.

Rules:
- Clinical vignette (patient age, sex, presentation, relevant labs/findings)
- 4-5 answer choices (A-E), exactly one correct, rest are plausible distractors
- Explanation in Thai: why correct answer is right AND why each distractor is wrong
- Include 1 pitfall (กับดักที่มักเข้าใจผิด)
- Base content ONLY on provided context — no hallucination
- Match the exam pattern style from KB reference

Output a single JSON object (NOT an array):
{
  "question": "A [age]-year-old [sex] presents with...",
  "options": ["A. ...", "B. ...", "C. ...", "D. ...", "E. ..."],
  "correctLetter": "A",
  "explanation": "อธิบายเป็นภาษาไทย...",
  "pitfall": "กับดัก: ...",
  "system": "Endocrine",
  "topic": "Hypercalcemia of malignancy"
}"""


def _generate_one(topic: str, system: str | None, kb_ctx: str, vault_ctx: str,
                  variation: int, seen_fps: set[str],
                  key_topics: str | None = None) -> dict | None:
    context = f"{kb_ctx}\n\n{vault_ctx}".strip()
    focus = f"\n\nFocus on these specific aspects from real exams:\n{key_topics}" if key_topics else ""
    user_msg = (
        f"Generate 1 MCQ about: {topic}"
        + (f"\nSystem: {system}" if system else "")
        + f"\nVariation #{variation} — use a different clinical scenario from previous questions."
        + focus
        + f"\n\nContext:\n{context}"
    )
    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg}
        ],
        format="json",
        options={"temperature": min(0.65 + variation * 0.05, 0.95),
                 "num_threads": OLLAMA_THREADS}
    )
    raw = response["message"]["content"].strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```json?\n?|```$", "", raw, flags=re.MULTILINE).strip()
    parsed = json.loads(raw)
    q = parsed if isinstance(parsed, dict) else (parsed[0] if isinstance(parsed, list) else None)
    if not q:
        return None

    # Dedup check
    fp = _fingerprint(q.get("question", ""))
    if fp in seen_fps:
        return None

    # Normalize
    letter = (q.get("correctLetter") or "").strip().upper()
    if letter not in "ABCDE" or not q.get("options") or len(q["options"]) < 4:
        return None

    explanation = q.get("explanation", "")
    if q.get("pitfall"):
        explanation += f"\n\n⚠️ **Pitfall:** {q['pitfall']}"

    return {
        "question":      q["question"],
        "options":       q["options"][:5],
        "correctIndex":  "ABCDE".index(letter),
        "correctLetter": letter,
        "explanation":   explanation,
        "system":        q.get("system") or system or "General",
        "mode":          "case",
        "topicKeyword":  q.get("topic") or topic,
        "createdAt":     datetime.now(timezone.utc).isoformat(),
        "source":        "examflow_local",
        "_fp":           fp,   # removed before push
    }


# ── Main runner ───────────────────────────────────────────────────────────────

def run(mode: str, topic: str | None, system: str | None,
        vault_folder: str | None, tier: str | None,
        n_per_topic: int, dry_run: bool):

    kb       = json.loads(Path(EXAM_KB_PATH).read_text(encoding="utf-8"))
    seen_fps = _load_log()

    # ── Build topic list ──────────────────────────────────────────────────────
    # Each item: {"disease", "system", "tier", "key_topics", "frequency"}
    topic_entries: list[dict] = []

    if mode == "topic":
        if not topic:
            sys.exit("[ERROR] --topic required for mode=topic")
        topic_entries = [{"disease": topic, "system": system,
                          "tier": "topic", "key_topics": None, "frequency": 1}]

    elif mode == "random":
        pool = _topics_from_kb(kb)
        if not pool:
            sys.exit("[ERROR] exam KB disease_index is empty")
        sample = random.sample(pool, min(5, len(pool)))
        topic_entries = [{"disease": t, "system": s, "tier": "random",
                          "key_topics": None, "frequency": 1} for t, s in sample]
        print(f"[quiz] Random: {[e['disease'] for e in topic_entries]}")

    elif mode == "kb-map":
        pairs = _topics_from_kb(kb, mode="all")
        topic_entries = [{"disease": t, "system": s, "tier": "kb",
                          "key_topics": None, "frequency": 1} for t, s in pairs]
        print(f"[quiz] KB-map: {len(topic_entries)} diseases")

    elif mode == "vault-map":
        pairs = _topics_from_vault(vault_folder)
        topic_entries = [{"disease": t, "system": s, "tier": "vault",
                          "key_topics": None, "frequency": 1} for t, s in pairs]
        print(f"[quiz] Vault-map: {len(topic_entries)} notes")

    elif mode == "high-yield":
        pairs = _topics_from_kb(kb, mode="high-yield")
        topic_entries = [{"disease": t, "system": s, "tier": "high_yield",
                          "key_topics": None, "frequency": 1} for t, s in pairs]
        print(f"[quiz] High-yield: {len(topic_entries)} diseases (freq ≥ 2)")

    elif mode == "scope":
        topic_entries = _topics_from_scope(tier)
        if not topic_entries:
            sys.exit("[ERROR] No scope entries found")
        print(f"[quiz] Scope-mode: {len(topic_entries)} diseases"
              + (f" (tier={tier})" if tier else ""))

    else:
        sys.exit(f"[ERROR] Unknown mode: {mode}")

    if not topic_entries:
        sys.exit("[ERROR] No topics found")

    token = None if dry_run else _firebase_token()
    total_pushed = 0
    new_fps: set[str] = set()

    for entry in topic_entries:
        topic_name = entry["disease"]
        topic_sys  = entry.get("system")
        key_topics = entry.get("key_topics")
        tier_label = entry.get("tier", "")

        tier_emoji = {"must_know": "🔴", "high_yield": "🟡",
                      "nice_to_know": "🟢"}.get(tier_label, "⚪")
        print(f"\n[quiz] {tier_emoji} {topic_name} ({topic_sys or 'auto'})")
        if key_topics:
            print(f"  แนว: {key_topics[:120]}")

        kb_ctx    = _kb_context(kb, topic_name, topic_sys)
        vault_ctx = _vault_context(topic_name)

        pushed_this = 0
        attempt     = 0
        while pushed_this < n_per_topic and attempt < n_per_topic * 3:
            attempt += 1
            print(f"  [{pushed_this+1}/{n_per_topic}]...", end=" ", flush=True)
            try:
                q = _generate_one(topic_name, topic_sys, kb_ctx, vault_ctx,
                                   attempt, seen_fps | new_fps,
                                   key_topics=key_topics)
                if not q:
                    print("SKIP (dup/invalid)")
                    continue

                fp = q.pop("_fp")

                if dry_run:
                    print("OK (dry)")
                    print(f"     {q['question'][:120]}")
                    print(f"     Answer: {q['correctLetter']}")
                else:
                    try:
                        doc_id = _push_firestore(q, token)
                    except Exception as push_err:
                        if "401" in str(push_err) or "403" in str(push_err):
                            token = _firebase_token()  # refresh
                            doc_id = _push_firestore(q, token)
                        else:
                            raise
                    print(f"OK → {doc_id[:12]}...")

                new_fps.add(fp)
                pushed_this += 1
                total_pushed += 1

            except Exception as e:
                print(f"ERROR: {e}")

    seen_fps.update(new_fps)
    _save_log(seen_fps)
    print(f"\n[quiz] Done: {total_pushed} questions {'(dry run)' if dry_run else 'pushed to Firebase'}")
    print(f"[quiz] Dedup log: {len(seen_fps)} fingerprints saved")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate MCQ → Firebase")
    parser.add_argument("--mode", choices=["topic", "random", "kb-map", "vault-map",
                                           "high-yield", "scope"],
                        default="topic")
    parser.add_argument("--topic",  help="Topic name (mode=topic only)")
    parser.add_argument("--system", help="Medical system filter")
    parser.add_argument("--folder", help="Obsidian subfolder (mode=vault-map, e.g. 'diseases')")
    parser.add_argument("--tier",   help="Scope tier filter: must_know / high_yield / nice_to_know")
    parser.add_argument("--n",      type=int, default=3, help="Questions per topic")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run(args.mode, args.topic, args.system, args.folder, args.tier, args.n, args.dry_run)
