"""
ExamFlow — Anki Export Pipeline
Converts disease summaries and vignettes → .apkg Anki deck
Usage: python examflow/anki_export.py [--output examflow/anki/ExamFlow.apkg]
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPORTS_DIR    = os.getenv("REPORTS_DIR", "./reports")
ANKI_OUTPUT    = os.getenv("ANKI_OUTPUT_PATH", "./examflow/anki/")
EXAM_KB_PATH   = os.getenv("EXAM_KB_PATH", "./examflow/exam_kb.json")

try:
    import genanki
    _GENANKI_AVAILABLE = True
except ImportError:
    _GENANKI_AVAILABLE = False


def _make_basic_model() -> "genanki.Model":
    return genanki.Model(
        1_380_001_001,
        "ExamFlow Basic",
        fields=[
            {"name": "Front"},
            {"name": "Back"},
            {"name": "Source"},
        ],
        templates=[{
            "name": "Card 1",
            "qfmt": "{{Front}}",
            "afmt": "{{FrontSide}}<hr id=answer>{{Back}}<br><small>{{Source}}</small>",
        }],
    )


def _make_cloze_model() -> "genanki.Model":
    return genanki.Model(
        1_380_001_002,
        "ExamFlow Cloze",
        fields=[
            {"name": "Text"},
            {"name": "Source"},
        ],
        templates=[{
            "name": "Cloze",
            "qfmt": "{{cloze:Text}}",
            "afmt": "{{cloze:Text}}<br><small>{{Source}}</small>",
        }],
        model_type=genanki.Model.CLOZE,
    )


def _extract_anki_cues(md_content: str) -> list[dict]:
    """Extract Q/A pairs from ## 🃏 Anki Cues section."""
    cards = []
    in_anki = False
    current_q = None
    source_line = ""

    for line in md_content.splitlines():
        if "Anki Cues" in line:
            in_anki = True
            continue
        if in_anki and line.startswith("##"):
            break
        if in_anki:
            if line.startswith("Q:"):
                current_q = line[2:].strip()
            elif line.startswith("A:") and current_q:
                cards.append({"q": current_q, "a": line[2:].strip()})
                current_q = None
        if line.startswith("Sources:"):
            source_line = line

    return cards, source_line


def _extract_must_know_cloze(md_content: str) -> list[str]:
    """Extract Must-Know Facts as cloze candidates."""
    cloze_texts = []
    in_section = False

    for line in md_content.splitlines():
        if "Must-Know Facts" in line:
            in_section = True
            continue
        if in_section and line.startswith("##"):
            break
        if in_section and line.strip().startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "-")):
            # Bold text → cloze candidate
            converted = re.sub(r'\*\*(.+?)\*\*', r'{{c1::\1}}', line.strip().lstrip("0123456789.-• "))
            if "{{c1::" in converted:
                cloze_texts.append(converted)

    return cloze_texts


def build_deck_from_reports(output_path: str) -> int:
    """Build Anki deck from all disease_*.md and vignette_*.md files."""
    if not _GENANKI_AVAILABLE:
        print("[ERROR] genanki not installed. Run: pip install genanki")
        return 0

    basic_model = _make_basic_model()
    cloze_model = _make_cloze_model()
    deck = genanki.Deck(2_380_001_001, "ExamFlow — Medical Exam Prep")
    notes_added = 0

    # Process disease summary files
    disease_files = glob.glob(os.path.join(REPORTS_DIR, "disease_*.md"))
    vignette_files = glob.glob(os.path.join(REPORTS_DIR, "vignette_*.md"))

    for md_path in disease_files + vignette_files:
        filename = os.path.basename(md_path)
        is_vignette = filename.startswith("vignette_")

        with open(md_path, encoding="utf-8") as f:
            content = f.read()

        if is_vignette:
            # Vignette → one Basic card (Case + Question as Front, Answer as Back)
            case_match   = re.search(r'\*\*Case:\*\*\n(.+?)(?=\*\*Question|\n---)', content, re.DOTALL)
            q_match      = re.search(r'\*\*Question:\*\*\s*(.+?)(?=\nA\.)', content, re.DOTALL)
            answer_match = re.search(r'\*\*Answer:\s*([A-E])\*\*', content)
            reason_match = re.search(r'\*\*เหตุผล:\*\*\n(.+?)(?=\*\*ทำไมตัวอื่นผิด|\n---)', content, re.DOTALL)

            if case_match and answer_match:
                front = f"{case_match.group(1).strip()}\n\n{q_match.group(1).strip() if q_match else ''}"
                back  = f"Answer: {answer_match.group(1)}\n\n{reason_match.group(1).strip() if reason_match else ''}"
                note  = genanki.Note(
                    model=basic_model,
                    fields=[front, back, filename],
                    tags=["ExamFlow", "vignette"]
                )
                deck.add_note(note)
                notes_added += 1
        else:
            # Disease file → Anki Cues + Cloze cards
            cue_cards, source_line = _extract_anki_cues(content)
            for card in cue_cards:
                note = genanki.Note(
                    model=basic_model,
                    fields=[card["q"], card["a"], source_line],
                    tags=["ExamFlow", "disease_summary"]
                )
                deck.add_note(note)
                notes_added += 1

            cloze_texts = _extract_must_know_cloze(content)
            for text in cloze_texts:
                note = genanki.Note(
                    model=cloze_model,
                    fields=[text, source_line],
                    tags=["ExamFlow", "must_know", "cloze"]
                )
                deck.add_note(note)
                notes_added += 1

    if notes_added > 0:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        genanki.Package(deck).write_to_file(output_path)
        print(f"[ExamFlow] Anki deck saved: {output_path} ({notes_added} cards)")
    else:
        print("[ExamFlow] No cards found. Generate disease summaries first (Branch G3).")

    return notes_added


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ExamFlow Anki Export")
    parser.add_argument(
        "--output",
        default=os.path.join(ANKI_OUTPUT, f"ExamFlow_{datetime.now().strftime('%Y%m%d')}.apkg"),
        help="Output .apkg path"
    )
    args = parser.parse_args()
    count = build_deck_from_reports(args.output)
    sys.exit(0 if count >= 0 else 1)
