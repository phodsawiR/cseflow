"""
context_enricher.py — KG + Vault graph context enrichment for session.py / vault_builder.py

enrich(text)              → KG facts + vault notes → context string (for sources)
get_kg_drug_flags(text)   → contraindication warnings → string (for challenger)
"""
import re
from typing import Optional

# ── Lazy singletons ───────────────────────────────────────────────────────────

_kg: Optional["ClinicalKG"] = None
_vault_notes: Optional[list[str]] = None


def _get_kg() -> Optional["ClinicalKG"]:
    global _kg
    if _kg is None:
        try:
            from kg_query import ClinicalKG
            _kg = ClinicalKG()
        except Exception as e:
            print(f"[enricher] KG unavailable: {e}")
            _kg = False  # type: ignore  # sentinel: don't retry
    return _kg if _kg is not False else None


def _get_vault_notes() -> list[str]:
    global _vault_notes
    if _vault_notes is None:
        try:
            from vault_graph import list_note_names
            _vault_notes = [n.lower().replace("_", " ") for n in list_note_names()]
        except Exception:
            _vault_notes = []
    return _vault_notes


# ── Entity patterns ───────────────────────────────────────────────────────────

_DISEASES = [
    "heart failure", "chf", "acute heart failure",
    "acs", "myocardial infarction", "stemi", "nstemi", "unstable angina",
    "atrial fibrillation", "afib", "supraventricular tachycardia", "svt",
    "hypertension", "hypertensive crisis",
    "copd", "aecopd", "asthma",
    "community acquired pneumonia", "pneumonia", "cap",
    "pulmonary embolism", "dvt",
    "acute kidney injury", "aki", "chronic kidney disease", "ckd",
    "sepsis", "septic shock",
    "meningitis", "encephalitis",
    "dka", "hhs", "diabetic ketoacidosis",
    "diabetes mellitus", "type 2 diabetes", "t2dm",
    "hypothyroidism", "hyperthyroidism", "thyroid storm",
    "hyponatremia", "hypernatremia", "hyperkalemia", "hypokalemia",
    "metabolic acidosis", "respiratory acidosis",
    "stroke", "tia", "ischemic stroke",
    "liver cirrhosis", "hepatic encephalopathy",
    "upper gi bleeding", "lower gi bleeding", "gi bleeding",
    "pleural effusion", "pneumothorax",
    "pancreatitis", "cholangitis", "cholecystitis",
]

_DRUGS = [
    "furosemide", "torsemide", "spironolactone", "hydrochlorothiazide",
    "metoprolol", "carvedilol", "bisoprolol", "atenolol",
    "lisinopril", "enalapril", "ramipril", "perindopril",
    "losartan", "valsartan", "candesartan", "sacubitril",
    "warfarin", "heparin", "enoxaparin", "rivaroxaban", "apixaban", "dabigatran",
    "aspirin", "clopidogrel", "ticagrelor", "prasugrel",
    "metformin", "insulin", "glipizide", "glibenclamide", "sitagliptin",
    "empagliflozin", "dapagliflozin", "liraglutide", "semaglutide",
    "amlodipine", "nifedipine", "diltiazem", "verapamil",
    "atorvastatin", "rosuvastatin", "simvastatin",
    "amiodarone", "digoxin", "adenosine",
    "vancomycin", "meropenem", "ceftriaxone", "piperacillin", "tazobactam",
    "azithromycin", "levofloxacin", "doxycycline",
    "omeprazole", "pantoprazole", "esomeprazole", "octreotide",
    "dexamethasone", "hydrocortisone", "methylprednisolone", "prednisolone",
    "morphine", "fentanyl", "tramadol",
    "norepinephrine", "dopamine", "dobutamine", "epinephrine", "vasopressin",
    "alteplase", "streptokinase",
    "nicardipine", "nitroprusside", "nitroglycerin",
    "phenytoin", "levetiracetam", "diazepam", "lorazepam",
    "naloxone", "flumazenil", "n-acetylcysteine",
    "allopurinol", "colchicine",
]


def extract_entities(text: str) -> dict[str, list[str]]:
    """Extract disease and drug mentions from patient text."""
    lower = text.lower()
    diseases = sorted({d for d in _DISEASES if d in lower}, key=len, reverse=True)
    drugs    = sorted({d for d in _DRUGS    if d in lower}, key=len, reverse=True)

    # Also match vault note names (catches topic-specific notes the student has written)
    vault_matches = [n for n in _get_vault_notes() if len(n) > 4 and n in lower]

    # De-duplicate: remove shorter entries that are substrings of longer ones
    def dedup(lst: list[str]) -> list[str]:
        result = []
        for item in lst:
            if not any(item != other and item in other for other in lst):
                result.append(item)
        return result

    return {
        "diseases":      dedup(diseases)[:8],
        "drugs":         dedup(drugs)[:8],
        "vault_matches": list(dict.fromkeys(vault_matches))[:6],
    }


# ── KG context ────────────────────────────────────────────────────────────────

def _kg_context(entities: dict) -> str:
    kg = _get_kg()
    if not kg:
        return ""

    parts = []
    all_clinical = entities["diseases"][:4] + entities["drugs"][:4]

    for entity in all_clinical:
        ctx = kg.get_kg_context(entity)
        if ctx:
            parts.append(ctx)

    return "\n\n".join(parts)


# ── Vault context ─────────────────────────────────────────────────────────────

def _vault_context(entities: dict) -> str:
    topics = entities["diseases"] + entities["drugs"] + entities["vault_matches"]
    if not topics:
        return ""
    try:
        from vault_graph import get_vault_context
        return get_vault_context(list(dict.fromkeys(topics))[:8], depth=1)
    except Exception:
        return ""


# ── Public API ────────────────────────────────────────────────────────────────

def enrich(patient_text: str) -> str:
    """
    Extract clinical entities → query KG + Obsidian vault → combined context string.
    Returns "" if nothing found (safe to call unconditionally).
    """
    entities = extract_entities(patient_text)
    if not any(entities.values()):
        return ""

    parts = []

    kg_ctx = _kg_context(entities)
    if kg_ctx:
        parts.append(kg_ctx)

    vault_ctx = _vault_context(entities)
    if vault_ctx:
        parts.append(vault_ctx)

    if not parts:
        return ""

    header = (
        "\n\n" + "─" * 50 +
        "\n[STRUCTURED KNOWLEDGE: KG + Vault]\n" +
        "─" * 50 + "\n\n"
    )
    return header + "\n\n---\n\n".join(parts)


# Abbreviation → full name expansion for KG matching
_DISEASE_ALIASES: dict[str, list[str]] = {
    "dka":  ["diabetic ketoacidosis"],
    "hhs":  ["hyperosmolar hyperglycemic state", "hyperglycemic hyperosmolar"],
    "aki":  ["acute kidney injury", "acute renal failure"],
    "ckd":  ["chronic kidney disease", "chronic renal failure"],
    "hf":   ["heart failure", "congestive heart failure"],
    "chf":  ["congestive heart failure", "heart failure"],
    "afib": ["atrial fibrillation"],
    "af":   ["atrial fibrillation"],
    "acs":  ["acute coronary syndrome"],
    "mi":   ["myocardial infarction"],
    "pe":   ["pulmonary embolism"],
    "dvt":  ["deep vein thrombosis"],
    "copd": ["chronic obstructive pulmonary disease"],
    "cap":  ["community acquired pneumonia", "pneumonia"],
    "svt":  ["supraventricular tachycardia"],
    "htn":  ["hypertension", "hypertensive"],
    "dm":   ["diabetes mellitus"],
    "t2dm": ["diabetes mellitus", "type 2 diabetes"],
}


def _expand_disease(name: str) -> list[str]:
    """Return the disease name + all known aliases for KG matching."""
    return [name] + _DISEASE_ALIASES.get(name, [])


def get_kg_drug_flags(patient_text: str) -> str:
    """
    Check extracted drugs against KG contraindications for identified diseases.
    Returns warning flags for challenger agent, or "" if none.
    """
    kg = _get_kg()
    if not kg:
        return ""

    entities = extract_entities(patient_text)
    drugs    = entities["drugs"]
    diseases = entities["diseases"]

    if not drugs or not diseases:
        return ""

    # Expand abbreviations for KG matching
    expanded_diseases: list[str] = []
    for d in diseases:
        expanded_diseases.extend(_expand_disease(d))
    expanded_diseases = list(dict.fromkeys(expanded_diseases))  # deduplicate

    flags = []
    for drug in drugs[:6]:
        cis = kg.get_contraindications(drug)
        for ci in cis:
            ci_lower = ci.lower()
            for disease in expanded_diseases:
                if disease in ci_lower or ci_lower in disease:
                    flags.append(f"KG: {drug} contraindicated in '{ci}'")
                    break

    if not flags:
        return ""

    return (
        "\n\n[KG DRUG SAFETY FLAGS — verify before accepting plan]\n" +
        "\n".join(f"  ⚠️  {f}" for f in flags[:8])
    )
