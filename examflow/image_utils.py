"""
image_utils.py — Shared image lookup for vault_builder + lecture_bridge

Priority:
  1. topic_images map (disease → known finding keys)
  2. findings static lookup (key → Wikimedia URL)
  3. researcher agent fallback (Gemini grounded search)
"""
import json
import os
from pathlib import Path

_IMAGES_PATH = Path(__file__).parent / "medical_images.json"


def load_image_db() -> dict:
    try:
        with open(_IMAGES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"findings": {}, "topic_images": {}}


def get_images_for_topic(topic: str) -> list[dict]:
    """
    คืน list ของ {finding, url, caption} สำหรับ topic นั้น
    - เช็ค topic_images map ก่อน
    - lookup URL จาก findings
    - url=None หมายถึงต้องหาจาก researcher
    """
    db = load_image_db()
    findings_db = db.get("findings", {})
    topic_map = db.get("topic_images", {})

    # exact match ก่อน ถ้าไม่เจอลอง partial
    keys = topic_map.get(topic)
    if not keys:
        topic_l = topic.lower()
        for k, v in topic_map.items():
            if k.lower() in topic_l or topic_l in k.lower():
                keys = v
                break

    if not keys:
        return []

    result = []
    for key in keys:
        entry = findings_db.get(key, {})
        result.append({
            "finding": key,
            "url": entry.get("url"),
            "caption": entry.get("caption", key),
        })
    return result


def resolve_missing_urls(images: list[dict]) -> list[dict]:
    """ใช้ researcher agent หา URL สำหรับ image ที่ url=None"""
    missing = [img for img in images if not img["url"]]
    if not missing:
        return images

    try:
        from router import call_agent
        names = [img["finding"] for img in missing]
        prompt = f"""Find Wikimedia Commons image URLs for these medical scores/findings.
Return DIRECT image URL from commons.wikimedia.org/wiki/Special:FilePath/...
Prefer table/diagram images for scoring systems. Return null if not found.

Items: {json.dumps(names)}

Return JSON only:
{{"results": [{{"finding": "...", "url": "https://..." or null}}]}}"""

        raw = call_agent("researcher", prompt=prompt)
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        url_map = {r["finding"].lower(): r.get("url") for r in data.get("results", [])}
        for img in missing:
            img["url"] = url_map.get(img["finding"].lower())
    except Exception as e:
        print(f"  [images] researcher fallback failed: {e}")

    return images


def format_image_block(images: list[dict]) -> str:
    """แปลง image list เป็น markdown block พร้อมแปะใน note"""
    lines = []
    for img in images:
        if not img.get("url"):
            continue
        lines.append(f"> ![{img['caption']}]({img['url']})")
    if not lines:
        return ""
    return "> [!note] ภาพอ้างอิง\n" + "\n".join(lines)


def get_image_context_for_prompt(topic: str, resolve: bool = True) -> str:
    """
    คืน string พร้อมใช้ใส่ใน analyzer prompt
    บอก LLM ว่ามีรูปอะไรให้ embed และ embed ยังไง
    """
    images = get_images_for_topic(topic)
    if not images:
        return ""
    if resolve:
        images = resolve_missing_urls(images)

    available = [img for img in images if img.get("url")]
    if not available:
        return ""

    lines = ["=== Visual References (embed these in the note) ===",
             "ใส่รูปเหล่านี้ในส่วนที่เกี่ยวข้อง โดยใช้ format:",
             "> [!note] ภาพอ้างอิง",
             "> ![caption](url)",
             ""]
    for img in available:
        lines.append(f'- **{img["finding"]}**: ![{img["caption"]}]({img["url"]})')
    return "\n".join(lines)
