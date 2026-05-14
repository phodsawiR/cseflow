import asyncio
from pathlib import Path
import sys
from knowledge_pipeline.ingest_knowledge import KnowledgeIngestor

async def main():
    print("Initializing ingestor...")
    ingestor = KnowledgeIngestor()
    
    # search in subfolders
    print("Searching for MD files...")
    candidates = list(Path("temp_ingest/marker_output").rglob("*.md"))
    if not candidates:
        print("MD not found, needs conversion.")
        return
    
    md_path = candidates[0]
    print(f"Found MD at {md_path} ({md_path.stat().st_size} bytes)")

    metadata = {
        "Specialty": "General_Practice",
        "Type": "Textbook",
        "Title": "Goldman-Cecil Medicine",
        "Year": 2020
    }
    
    print("Splitting document... (this might take a minute)")
    chunks = ingestor.split_document(md_path, "Textbook")
    print(f"Split into {len(chunks)} chunks.")
    
    if len(chunks) == 0:
        print("No chunks created. Check split logic.")
        return

    print("Saving chunks to disk...")
    saved_files = ingestor.save_chunks(chunks, metadata, "Goldman-Cecil Medicine")
    print(f"Saved {len(saved_files)} files.")
    
    print("Uploading to NotebookLM... (this will take time)")
    try:
        await ingestor.upload_to_notebooklm(saved_files, metadata, auto_create=True)
        print("Upload completed.")
    except Exception as e:
        print(f"Upload failed: {e}")
        return
    
    print("Updating index...")
    ingestor.update_index("Goldman-Cecil Medicine", saved_files, metadata)
    print("Process finished successfully!")

if __name__ == "__main__":
    asyncio.run(main())
