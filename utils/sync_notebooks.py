import asyncio
import json
import os
from pathlib import Path
from notebooklm import NotebookLMClient

NOTEBOOKS_FILE = Path("notebooks.json")

# รายชื่อ Notebook ที่เราต้องการให้มีในระบบ
REQUIRED_NOTEBOOKS = [
    "cardiology",
    "nephrology",
    "endocrine",
    "pulmonology",
    "infectious_disease",
    "pharmacology",
    "symptom_approach",
    "interpretation"
]

def load_notebooks():
    if NOTEBOOKS_FILE.exists():
        with open(NOTEBOOKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_notebooks(data):
    with open(NOTEBOOKS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

async def sync_notebooks():
    print("🔄 Syncing NotebookLM IDs...")
    
    current_data = load_notebooks()
    
    try:
        async with await NotebookLMClient.from_storage() as client:
            # 1. ดึงรายชื่อ Notebook ทั้งหมดที่มีใน Account
            print("📡 Fetching existing notebooks from account...")
            remote_notebooks = await client.notebooks.list()
            
            # สร้าง map จาก title -> id (normalize title เป็น lowercase)
            remote_map = {nb.title.lower().replace(" ", "_"): nb.id for nb in remote_notebooks}
            
            updated = False
            for key in REQUIRED_NOTEBOOKS:
                # ถ้าใน notebooks.json ยังไม่มี ID หรือเป็น placeholder
                if current_data.get(key) in [None, "NOTEBOOK_ID_HERE", ""]:
                    # ลองหาจากเครื่องที่มีอยู่แล้ว
                    if key in remote_map:
                        print(f"✅ Found existing notebook for '{key}': {remote_map[key]}")
                        current_data[key] = remote_map[key]
                        updated = True
                    else:
                        # ถ้าไม่มี ให้สร้างใหม่
                        print(f"➕ Creating new notebook for '{key}'...")
                        try:
                            # ใช้ title ที่ดูดีหน่อย เช่น cardiology -> Cardiology
                            new_title = key.replace("_", " ").title()
                            new_nb = await client.notebooks.create(new_title)
                            current_data[key] = new_nb.id
                            print(f"✨ Created: {new_nb.id}")
                            updated = True
                        except Exception as e:
                            print(f"❌ Failed to create '{key}': {e}")
                else:
                    print(f"ℹ️  '{key}' already has ID: {current_data[key]}")

            if updated:
                save_notebooks(current_data)
                print(f"💾 Updated {NOTEBOOKS_FILE} successfully.")
            else:
                print("✅ All notebooks are already synced.")
                
    except Exception as e:
        print(f"❌ Error during sync: {e}")
        if "Authentication expired" in str(e):
            print("🔑 Please run 'notebooklm login' in your terminal first.")

if __name__ == "__main__":
    asyncio.run(sync_notebooks())
