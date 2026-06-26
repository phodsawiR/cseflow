# Colorize Vault — Instructions for Claude Code (Remote PC)

คำสั่งนี้ให้ Claude Code รันบน PC เครื่องอื่นเพื่อใส่สีใน Obsidian notes ด้วย Ollama

---

## สิ่งที่ต้องมีก่อน

1. **Ollama** ติดตั้งแล้วและรันอยู่
2. **qwen2.5:14b** pull ไว้แล้ว
3. **Obsidian vault** sync มาแล้ว (Obsidian Sync / Google Drive / โฟลเดอร์ copy)
4. **Python 3.10+** ติดตั้งแล้ว

---

## Step 1 — Setup

```bash
# ติดตั้ง Ollama (ถ้ายังไม่มี)
# Windows: https://ollama.com/download
# แล้ว pull model:
ollama pull qwen2.5:14b

# ติดตั้ง Python dependency เดียวที่ต้องการ
pip install python-dotenv
```

---

## Step 2 — copy script

copy ไฟล์ `colorize_vault.py` และ `add_mastery.py` จาก caseflow repo มาไว้ที่ไหนก็ได้

สร้าง `.env` ข้างๆ ไฟล์:

```env
OBSIDIAN_VAULT_PATH=C:/path/to/Obsidian vault
```

แก้ path ให้ตรงกับ vault บนเครื่องนี้

---

## Step 3 — รัน add_mastery ก่อน (เร็ว ไม่ต้อง LLM)

```bash
python add_mastery.py --dry-run   # preview
python add_mastery.py             # รันจริง ใช้เวลา < 10 วินาที
```

---

## Step 4 — รัน colorize

```bash
# dry-run ก่อนเสมอ
python colorize_vault.py --dry-run

# รันเฉพาะ folder เดียวก่อน (แนะนำ)
python colorize_vault.py --folder "02 - Diseases" --workers 2

# รันทุก folder
python colorize_vault.py --workers 2
```

---

## Flags

| Flag | Default | ความหมาย |
|---|---|---|
| `--folder` | ทั้ง vault | เฉพาะ subfolder นี้ |
| `--workers` | 2 | parallel requests ไป Ollama |
| `--dry-run` | off | preview ไม่เขียนไฟล์ |
| `--force` | off | ใส่สีซ้ำแม้มีอยู่แล้ว |
| `--model` | auto | ระบุ Ollama model เอง |

---

## ถ้า Ollama อยู่เครื่องอื่น (remote)

แก้ใน `colorize_vault.py`:
```python
OLLAMA_URL = "http://192.168.x.x:11434"
```

บนเครื่อง Ollama รันด้วย:
```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

---

## ความเร็วโดยประมาณ

| Model | Hardware | เวลา/note |
|---|---|---|
| qwen2.5:14b | CPU only | ~3-5 นาที |
| qwen2.5:14b | GPU (8GB VRAM) | ~10-20 วินาที |
| qwen2.5:7b | CPU only | ~1-2 นาที |
| llama3.2:3b | CPU only | ~20-30 วินาที (คุณภาพต่ำกว่า) |

**แนะนำ:** ถ้าไม่มี GPU ใช้ `--workers 1` แล้วปล่อยทิ้งข้ามคืน

---

## Backup

ทุกไฟล์จะถูก backup อัตโนมัติก่อนแก้ไว้ที่:
```
[Obsidian vault]/.colorize_backup/
```
