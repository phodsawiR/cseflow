# CaseFlow + Obsidian Vault — System Context

You are an AI assistant managing a medical knowledge system for a Thai Year-4 medical student.
You have --yolo mode and can execute file system operations, run scripts, and manage files directly.

## Directory Layout

- **Project root**: `C:\Users\USER\OneDrive\Desktop\caseflow\` (current working directory)
- **Obsidian Vault**: `./obsidian/` (symlink — all vault files live here)
- **Vault manifest**: `./vault_manifest.txt` (full file list — check here before searching)

## Vault Folder Structure

```
obsidian/
├── 00 - Inbox/   ← 1 note
├── 01 - Active Cases/   ← 0 notes
├── 02 - Diseases/   ← 1 note
├── 03 - Drugs/   ← 0 notes
├── 04 - Labs/   ← 4 notes
├── 05 - Films/   ← 0 notes
├── 06 - Guidelines/   ← 0 notes
├── 07 - Procedures/   ← 5 notes
├── 08 - Approaches/   ← 11 notes
├── 99 - Templates/   ← 8 notes
└── copilot/   ← 0 notes
```

## CaseFlow Scripts

| Script | Purpose | Example usage |
|--------|---------|---------------|
| `vault_builder.py` | Generate Obsidian notes with AI | `python vault_builder.py --topic "Sepsis" --type diseases` |
| `vault_linker.py` | Add [[wikilinks]] between notes | `python vault_linker.py` |
| `migrate_vault.py` | Migrate/reorganize vault structure | `python migrate_vault.py` |
| `knowledge_pipeline/ingest_knowledge.py` | Ingest PDF → ChromaDB RAG + GDrive | `python -m knowledge_pipeline.ingest_knowledge --file book.pdf` |

### vault_builder.py flags
- `--topic "Name"` — single topic (ALWAYS use this)
- `--type` — diseases / approaches / drugs / labs (default: diseases)
- `--only diseases` — all topics of one type
- `--all` — everything (**ask for confirmation first — expensive**)
- `--resume "Topic"` — resume after error

## Knowledge RAG System

PDF textbooks/guidelines are indexed into **ChromaDB** (local vector DB) and optionally backed up to **Google Drive**.

- **RAG DB**: `knowledge_base/chroma_db/` — auto-created on first ingest
- **Chunks**: `knowledge_pipeline/knowledge_base/[Specialty]/[Type]/`
- **Query**: `knowledge_rag.py` → called automatically by `notebooklm_client.py` → used by vault_builder & session

### Google Drive backup (optional, one-time setup)
1. Create service account at console.cloud.google.com → enable Drive API
2. Download JSON key → save as `google-service-account.json` in project root
3. Set `GDRIVE_KNOWLEDGE_FOLDER_ID=<folder_id>` in `.env` (optional)

## Rules

1. Vault files → always use path under `./obsidian/`
2. Scripts → run from project root (current cwd)
3. NEVER run `vault_builder.py --all` without explicit user confirmation
4. Medical content targets Year-4 Thai medical students (ward medicine level)
5. Obsidian internal links use `[[Note Name]]` format
6. Check `vault_manifest.txt` before creating files to avoid duplicates
