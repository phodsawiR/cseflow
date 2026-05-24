"""
kg_filter.py — Filter PrimeKG to clinical-only subset
ลดจาก ~500MB เหลือ ~5MB เฉพาะ relations ที่ใช้จริงใน ward medicine
"""
import os
import sys
import pandas as pd


# Relations ที่ใช้จริงใน ward medicine
CLINICAL_RELATIONS = [
    "indication",                    # drug → treats disease
    "contraindication",              # drug → contraindicated
    "drug_effect",                   # drug → side effect
    "disease_phenotype_positive",    # disease → symptom
    "disease_disease",               # disease → comorbidity
    "off-label use",                 # drug → off-label
]


def filter_clinical_kg(input_path: str, output_path: str):
    """
    Filter PrimeKG เหลือเฉพาะ clinical relationships
    """
    print("Loading PrimeKG...")
    kg = pd.read_csv(input_path, low_memory=False)

    print(f"Total edges: {len(kg):,}")
    print(f"Available relations: {kg['relation'].unique().tolist()}")

    # Filter เฉพาะ clinical relations
    clinical = kg[kg["relation"].isin(CLINICAL_RELATIONS)].copy()

    print(f"Clinical edges: {len(clinical):,}")
    print(f"Relations kept: {clinical['relation'].value_counts().to_dict()}")

    # Save
    clinical.to_csv(output_path, index=False)
    print(f"✅ Saved to {output_path} ({os.path.getsize(output_path) / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    # Default paths
    script_dir = os.path.dirname(os.path.abspath(__file__))

    input_path = os.path.join(script_dir, "kg.csv")
    output_path = os.path.join(script_dir, "clinical_kg.csv")

    if len(sys.argv) >= 2:
        input_path = sys.argv[1]
    if len(sys.argv) >= 3:
        output_path = sys.argv[2]

    if not os.path.exists(input_path):
        print(f"❌ ไม่พบไฟล์ {input_path}")
        print()
        print("วิธีดาวน์โหลด PrimeKG:")
        print("  1. ไปที่ https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/IXA7BM")
        print("  2. ดาวน์โหลดไฟล์ kg.csv (~500MB)")
        print("  3. วางไว้ในโฟลเดอร์เดียวกับสคริปต์นี้")
        print()
        print("หรือใช้คำสั่ง:")
        print('  curl -L -o kg.csv "https://dataverse.harvard.edu/api/access/datafile/6180620"')
        sys.exit(1)

    filter_clinical_kg(input_path, output_path)
