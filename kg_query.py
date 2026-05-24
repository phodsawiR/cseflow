"""
kg_query.py — Query clinical relationships from filtered PrimeKG
ใช้สำหรับ vault_linker.py เพื่อสร้าง evidence-based wikilinks
"""
import os
import pandas as pd
from typing import List, Dict


class ClinicalKG:
    """
    Query engine สำหรับ clinical knowledge graph (filtered PrimeKG)
    """

    def __init__(self, kg_path: str = None):
        if kg_path is None:
            kg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clinical_kg.csv")

        if not os.path.exists(kg_path):
            raise FileNotFoundError(
                f"ไม่พบ {kg_path}\n"
                "กรุณารัน kg_filter.py ก่อนเพื่อสร้างไฟล์ clinical_kg.csv"
            )

        print(f"Loading clinical KG from {kg_path}...")
        self.kg = pd.read_csv(kg_path, low_memory=False)
        print(f"Loaded {len(self.kg):,} clinical edges")

    def get_drugs(self, disease: str) -> List[str]:
        """หายาที่ใช้รักษาโรค (indication relationship)"""
        mask = (
            (self.kg["relation"] == "indication") &
            (self.kg["y_name"].str.contains(disease, case=False, na=False))
        )
        return self.kg[mask]["x_name"].tolist()

    def get_contraindications(self, drug: str) -> List[str]:
        """หา contraindications ของยา"""
        mask = (
            (self.kg["relation"] == "contraindication") &
            (self.kg["x_name"].str.contains(drug, case=False, na=False))
        )
        return self.kg[mask]["y_name"].tolist()

    def get_side_effects(self, drug: str) -> List[str]:
        """หา side effects ของยา (top 10)"""
        mask = (
            (self.kg["relation"] == "drug_effect") &
            (self.kg["x_name"].str.contains(drug, case=False, na=False))
        )
        return self.kg[mask]["y_name"].tolist()[:10]

    def get_symptoms(self, disease: str) -> List[str]:
        """หา symptoms/phenotypes ของโรค (top 10)"""
        mask = (
            (self.kg["relation"] == "disease_phenotype_positive") &
            (self.kg["x_name"].str.contains(disease, case=False, na=False))
        )
        return self.kg[mask]["y_name"].tolist()[:10]

    def get_comorbidities(self, disease: str) -> List[str]:
        """หาโรคที่มักเกิดร่วมกัน (top 5)"""
        mask = (
            (self.kg["relation"] == "disease_disease") &
            (self.kg["x_name"].str.contains(disease, case=False, na=False))
        )
        return self.kg[mask]["y_name"].tolist()[:5]

    def get_all_connections(self, entity: str) -> Dict[str, List[str]]:
        """Query ทุก relationships ของ entity นี้ (ทั้ง disease และ drug)"""
        return {
            "drugs": self.get_drugs(entity),
            "side_effects": self.get_side_effects(entity),
            "contraindications": self.get_contraindications(entity),
            "symptoms": self.get_symptoms(entity),
            "comorbidities": self.get_comorbidities(entity),
        }

    def get_kg_context(self, entity: str) -> str:
        """
        สร้าง text summary ของ KG connections สำหรับใช้เป็น context
        ใน vault_builder เพื่อให้ note มีข้อมูลจาก evidence-based KG
        """
        connections = self.get_all_connections(entity)
        parts = []

        if connections["drugs"]:
            parts.append(f"Drugs for {entity}: {', '.join(connections['drugs'][:8])}")
        if connections["symptoms"]:
            parts.append(f"Symptoms/Phenotypes: {', '.join(connections['symptoms'])}")
        if connections["comorbidities"]:
            parts.append(f"Common comorbidities: {', '.join(connections['comorbidities'])}")
        if connections["side_effects"]:
            parts.append(f"Side effects: {', '.join(connections['side_effects'])}")
        if connections["contraindications"]:
            parts.append(f"Contraindications: {', '.join(connections['contraindications'])}")

        if not parts:
            return ""

        return f"[PrimeKG Evidence for '{entity}']\n" + "\n".join(parts)


if __name__ == "__main__":
    # Quick test
    kg = ClinicalKG()
    print("\n--- Test: Heart Failure ---")
    ctx = kg.get_kg_context("heart failure")
    print(ctx if ctx else "(no results)")

    print("\n--- Test: Furosemide ---")
    ctx = kg.get_kg_context("furosemide")
    print(ctx if ctx else "(no results)")
