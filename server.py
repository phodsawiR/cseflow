import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional, List

import anthropic as _anthropic

import markdown as md_lib
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import shutil
from pathlib import Path

from session import CaseSession
from router import _current_session, _step_log
from knowledge_pipeline.ingest_knowledge import KnowledgeIngestor, TEMP_DIR, ingest_batch
from knowledge_pipeline.manifest import (
    parse_manifest_text, scan_inbox, INBOX_DIR
)
from examflow.ingest_exam import ingest_pdf as _ingest_exam_pdf

os.makedirs("static", exist_ok=True)

app = FastAPI(title="CaseFlow API", version="2.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_cache_control_header(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

sessions: dict[str, CaseSession] = {}
last_active: dict[str, datetime] = {}
processing_status: dict[str, str] = {}  # session_id -> "processing" | "done" | "error:..."
pending_kb_inputs = {}  # session_id -> asyncio.Future

SESSION_TIMEOUT = timedelta(hours=2)
PIPELINE_TIMEOUT = 300.0  # 5 minutes per LLM pipeline call


# ── Request models ─────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    input: str

class FeedbackRequest(BaseModel):
    session_id: str
    message: str

class ConfirmRequest(BaseModel):
    session_id: str
    message: str = "ยืนยัน"

class ApproveRequest(BaseModel):
    session_id: str

class QAReviseRequest(BaseModel):
    session_id: str
    instruction: str = ""


# ── Session helpers ─────────────────────────────────────────────────────────────

def _cleanup_expired():
    now = datetime.now()
    expired = [sid for sid, t in list(last_active.items()) if now - t > SESSION_TIMEOUT]
    for sid in expired:
        sessions.pop(sid, None)
        last_active.pop(sid, None)


def _touch(session_id: str):
    last_active[session_id] = datetime.now()


def _get_session(session_id: str) -> CaseSession:
    _cleanup_expired()
    if session_id.startswith("kb_"):
        # Dummy session for KB uploads to satisfy _get_session if needed, 
        # but better to handle it explicitly in endpoints
        return None
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    _touch(session_id)
    return session


def _build_response(session: CaseSession, **extra) -> dict:
    return {
        "session_id": session.session_id,
        "draft": session.draft,
        "qa_review": session.qa_review,
        "cost_summary": session.last_cost_summary,
        "branch": session.branch,
        "version": session.version,
        "needs_branch_confirm": session._pending_branch is not None,
        "pending_branch": session._pending_branch.get("branch") if session._pending_branch else None,
        "needs_confirm": session._pending_plan is not None,
        "error": None,
        **extra,
    }


# ── Endpoints ───────────────────────────────────────────────────────────────────

@app.post("/start")
async def start(req: StartRequest):
    session_id = str(uuid.uuid4())
    session = CaseSession(session_id=session_id)
    sessions[session_id] = session
    _touch(session_id)
    processing_status[session_id] = "processing"

    async def _run():
        token = _current_session.set(session_id)
        try:
            await asyncio.to_thread(session.start, req.input)
            processing_status[session_id] = "done"
        except Exception as e:
            processing_status[session_id] = f"error:{e}"
        finally:
            _current_session.reset(token)

    asyncio.create_task(_run())
    return {**_build_response(session), "processing": True}


@app.post("/feedback")
async def feedback(req: FeedbackRequest):
    session = _get_session(req.session_id)
    _step_log.pop(req.session_id, None)
    processing_status[req.session_id] = "processing"

    async def _run():
        token = _current_session.set(req.session_id)
        try:
            if session._pending_branch is not None:
                await asyncio.to_thread(session.confirm_branch, req.message)
            elif session._pending_plan is not None:
                await asyncio.to_thread(session.execute_plan, req.message)
            else:
                await asyncio.to_thread(session.revise, req.message)
            processing_status[req.session_id] = "done"
        except Exception as e:
            processing_status[req.session_id] = f"error:{e}"
        finally:
            _current_session.reset(token)

    asyncio.create_task(_run())
    _touch(req.session_id)
    return {**_build_response(session), "processing": True}


@app.post("/confirm")
async def confirm(req: ConfirmRequest):
    session = _get_session(req.session_id)
    if session._pending_branch is None and session._pending_plan is None:
        raise HTTPException(status_code=400, detail="No pending confirmation for this session")

    _step_log.pop(req.session_id, None)
    processing_status[req.session_id] = "processing"

    async def _run():
        token = _current_session.set(req.session_id)
        try:
            if session._pending_branch is not None:
                await asyncio.to_thread(session.confirm_branch, req.message or "ยืนยัน")
            else:
                await asyncio.to_thread(session.execute_plan, req.message or "ยืนยัน")
            processing_status[req.session_id] = "done"
        except Exception as e:
            processing_status[req.session_id] = f"error:{e}"
        finally:
            _current_session.reset(token)

    asyncio.create_task(_run())
    _touch(req.session_id)

    return {**_build_response(session), "processing": True}


@app.get("/result/{session_id}")
async def result(session_id: str):
    session = _get_session(session_id)
    job = processing_status.get(session_id, "done")
    if job == "processing":
        return {**_build_response(session), "processing": True}
    if job.startswith("error:"):
        return {**_build_response(session), "processing": False, "error": job[6:]}
    return {**_build_response(session), "processing": False}


@app.post("/qa_revise")
async def qa_revise(req: QAReviseRequest):
    session = _get_session(req.session_id)
    _step_log.pop(req.session_id, None)
    processing_status[req.session_id] = "processing"

    async def _run():
        token = _current_session.set(req.session_id)
        try:
            await asyncio.to_thread(session.qa_revise, req.instruction)
            processing_status[req.session_id] = "done"
        except Exception as e:
            processing_status[req.session_id] = f"error:{e}"
        finally:
            _current_session.reset(token)

    asyncio.create_task(_run())
    _touch(req.session_id)
    return {**_build_response(session), "processing": True}


@app.post("/approve")
async def approve(req: ApproveRequest):
    session = _get_session(req.session_id)
    try:
        filename = await asyncio.to_thread(session.approve)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "session_id": req.session_id,
        "saved": filename,
        "view_url": f"/view/{req.session_id}",
        "success": True,
        "error": None,
    }


@app.get("/status/{session_id}")
async def status(session_id: str):
    session = _get_session(session_id)
    return {
        "session_id": session_id,
        "branch": session.branch,
        "version": session.version,
        "has_draft": bool(session.draft),
        "needs_branch_confirm": session._pending_branch is not None,
        "pending_branch": session._pending_branch.get("branch") if session._pending_branch else None,
        "needs_confirm": session._pending_plan is not None,
    }


@app.get("/view/{session_id}", response_class=HTMLResponse)
async def view_report(session_id: str):
    session = _get_session(session_id)
    if not session.draft:
        raise HTTPException(status_code=404, detail="No draft available")

    body = md_lib.markdown(
        session.draft,
        extensions=["tables", "fenced_code", "nl2br"],
    )
    qa_block = ""
    if session.qa_review:
        qa_html = md_lib.markdown(session.qa_review, extensions=["tables", "fenced_code", "nl2br"])
        qa_block = f'<div class="qa-review"><h2>QA Review</h2>{qa_html}</div>'
    branch_labels = {
        "A": "Case Analysis", "B": "Knowledge Query", "C": "Symptom Approach",
        "D": "Progress Note", "E": "Morning Round", "F": "Interpreter",
        "G": "Admission Note", "H": "Note Blind Spot", "U": "Freestyle",
    }
    label = branch_labels.get(session.branch, session.branch)
    title = f"CaseFlow — {label} v{session.version}"

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Thai:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    body {{
      font-family: 'Noto Sans Thai', 'Sarabun', Arial, sans-serif;
      font-size: 14px; line-height: 1.8;
      max-width: 800px; margin: 0 auto; padding: 30px 20px;
      color: #2c3e50;
    }}
    h1 {{ font-size: 20px; border-bottom: 2px solid #2c3e50; padding-bottom: 8px; }}
    h2 {{ font-size: 16px; border-bottom: 1px solid #ccc; color: #2c3e50; padding-bottom: 4px; margin-top: 24px; }}
    h3 {{ font-size: 14px; margin-top: 14px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }}
    td, th {{ border: 1px solid #bbb; padding: 7px 10px; text-align: left; }}
    th {{ background: #f0f0f0; font-weight: 700; }}
    ul, ol {{ margin: 6px 0 6px 22px; }} li {{ margin-bottom: 3px; }}
    code {{ background: #f4f4f4; padding: 1px 5px; border-radius: 3px; font-size: 13px; }}
    pre {{ background: #f4f4f4; padding: 12px; border-radius: 6px; overflow-x: auto; }}
    blockquote {{ border-left: 3px solid #ccc; margin-left: 0; padding-left: 12px; color: #555; }}
    strong {{ font-weight: 700; }}

    /* Print toolbar — ซ่อนตอน print */
    #toolbar {{
      position: fixed; bottom: 20px; right: 20px;
      display: flex; gap: 10px; z-index: 999;
    }}
    #toolbar button {{
      padding: 10px 18px; font-size: 15px; font-family: inherit;
      border: none; border-radius: 8px; cursor: pointer; font-weight: 600;
    }}
    .btn-print {{ background: #2563eb; color: #fff; }}
    .btn-close {{ background: #e5e7eb; color: #374151; }}

    .qa-review {{
      margin-top: 40px; padding: 16px 20px;
      background: #fffbeb; border: 1px solid #f59e0b;
      border-left: 4px solid #f59e0b; border-radius: 6px;
    }}
    .qa-review h2 {{
      color: #92400e; border-bottom: 1px solid #f59e0b;
      font-size: 15px; margin-top: 0;
    }}
    .qa-review p, .qa-review li {{ font-size: 13px; color: #44403c; }}

    @media print {{
      #toolbar {{ display: none !important; }}
      body {{ margin: 0; padding: 1cm; max-width: none; }}
      h2 {{ page-break-before: auto; }}
      table {{ page-break-inside: avoid; }}
    }}
  </style>
</head>
<body>
  <div id="toolbar">
    <button class="btn-print" onclick="window.print()">🖨️ Print / Save PDF</button>
    <button class="btn-close" onclick="window.close()">✕ ปิด</button>
  </div>
  {body}
  {qa_block}
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/upload_kb")
async def upload_kb(
    file: Optional[UploadFile] = File(None),
    link: Optional[str] = Form(None),
    fast: bool = Form(False)
):
    session_id = f"kb_{str(uuid.uuid4())[:8]}"
    _step_log[session_id] = []
    processing_status[session_id] = "processing"

    def progress_callback(label, status, detail="", *, percent=None, current=None, total=None):
        # Update step log for the progress UI
        steps = _step_log.get(session_id, [])
        payload = {"label": label, "status": status, "detail": detail,
                   "percent": percent, "current": current, "total": total}
        # If last step has same label, update it, else append
        if steps and steps[-1]["label"] == label:
            steps[-1].update(payload)
        else:
            steps.append(payload)
        _step_log[session_id] = steps

    async def async_input_callback(prompt_text: str) -> str:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending_kb_inputs[session_id] = future
        progress_callback("รอการยืนยัน", "waiting_input", prompt_text)
        try:
            return await future
        finally:
            pending_kb_inputs.pop(session_id, None)

    async def _run_ingest():
        try:
            ingestor = KnowledgeIngestor(progress_callback=progress_callback, async_input_callback=async_input_callback)
            pdf_path = None
            source_ref = ""

            if file:
                source_ref = file.filename
                dest = TEMP_DIR / file.filename
                TEMP_DIR.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as buffer:
                    shutil.copyfileobj(file.file, buffer)
                pdf_path = dest
            elif link:
                source_ref = link
                pdf_path = await ingestor.crawl_for_pdf(link)

            if not pdf_path:
                processing_status[session_id] = "error:No PDF obtained"
                return

            source_name = pdf_path.stem
            # Run sync, CPU-heavy steps in a thread so the event loop stays
            # responsive to /progress polling.
            md_path = await asyncio.to_thread(ingestor.convert_pdf_to_md, pdf_path, fast)
            if not md_path:
                processing_status[session_id] = "error:Conversion failed"
                return

            metadata = await asyncio.to_thread(ingestor.detect_metadata, md_path)
            chunks = await asyncio.to_thread(
                ingestor.split_document, md_path, str(metadata.get("Type"))
            )
            saved_files = await asyncio.to_thread(
                ingestor.save_chunks, chunks, metadata, source_name, source_ref
            )

            await ingestor.upload_to_notebooklm(saved_files, metadata)
            ingestor.update_index(source_name, saved_files, metadata)
            
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
            processing_status[session_id] = "done"
        except Exception as e:
            processing_status[session_id] = f"error:{str(e)}"
            progress_callback("เกิดข้อผิดพลาด", "error", str(e))

    asyncio.create_task(_run_ingest())
    return {"session_id": session_id, "processing": True}


class KBInputRequest(BaseModel):
    session_id: str
    message: str

@app.post("/kb_input")
async def kb_input(req: KBInputRequest):
    future = pending_kb_inputs.get(req.session_id)
    if not future:
        raise HTTPException(status_code=400, detail="No pending input for this session")
    future.set_result(req.message)
    return {"success": True}

# ── Batch ingestion (manifest + inbox) ──────────────────────────────────────────

class BatchManifestRequest(BaseModel):
    manifest_text: str
    auto_create: bool = True
    no_upload: bool = False
    fast: bool = True


def _make_progress_callback(session_id: str):
    def cb(label, status, detail="", *, percent=None, current=None, total=None):
        steps = _step_log.get(session_id, [])
        payload = {"label": label, "status": status, "detail": detail,
                   "percent": percent, "current": current, "total": total}
        if steps and steps[-1]["label"] == label:
            steps[-1].update(payload)
        else:
            steps.append(payload)
        _step_log[session_id] = steps
    return cb

def _make_async_input_callback(session_id: str, progress_callback):
    async def async_input_callback(prompt_text: str) -> str:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending_kb_inputs[session_id] = future
        progress_callback("รอการยืนยัน", "waiting_input", prompt_text)
        try:
            return await future
        finally:
            pending_kb_inputs.pop(session_id, None)
    return async_input_callback

@app.post("/upload_kb_batch")
async def upload_kb_batch(req: BatchManifestRequest):
    """Run ingest_batch from a YAML manifest sent as text body."""
    try:
        specs = parse_manifest_text(req.manifest_text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Manifest parse error: {e}")

    if not specs:
        raise HTTPException(status_code=400, detail="Manifest contains no valid sources")

    session_id = f"kb_{str(uuid.uuid4())[:8]}"
    _step_log[session_id] = []
    processing_status[session_id] = "processing"
    cb = _make_progress_callback(session_id)
    input_cb = _make_async_input_callback(session_id, cb)
    cb(f"เริ่ม batch ({len(specs)} sources)", "running")

    async def _run():
        try:
            summary = await ingest_batch(
                specs,
                no_upload=req.no_upload,
                auto_create=req.auto_create,
                fast=req.fast,
                progress_callback=cb,
                async_input_callback=input_cb,
            )
            cb(f"Batch เสร็จ — {summary['ok']} ok / {summary['failed']} failed", "done")
            processing_status[session_id] = "done"
        except Exception as e:
            cb("Batch error", "error", str(e))
            processing_status[session_id] = f"error:{e}"

    asyncio.create_task(_run())
    return {"session_id": session_id, "total": len(specs), "processing": True}


@app.get("/scan_inbox")
async def get_inbox_listing():
    """List PDFs in ./inbox/ folder."""
    specs = scan_inbox(INBOX_DIR)
    return {
        "inbox_dir": str(INBOX_DIR.resolve()),
        "count": len(specs),
        "files": [Path(s["file"]).name for s in specs],
    }


class InboxIngestRequest(BaseModel):
    auto_create: bool = True
    no_upload: bool = False
    fast: bool = True
    archive: bool = True


@app.post("/upload_kb_inbox")
async def upload_kb_inbox(req: InboxIngestRequest):
    """Scan ./inbox/ and ingest every PDF found."""
    specs = scan_inbox(INBOX_DIR)
    if not specs:
        raise HTTPException(status_code=400, detail=f"No PDFs found in {INBOX_DIR}")

    session_id = f"kb_{str(uuid.uuid4())[:8]}"
    _step_log[session_id] = []
    processing_status[session_id] = "processing"
    cb = _make_progress_callback(session_id)
    input_cb = _make_async_input_callback(session_id, cb)
    cb(f"เริ่ม inbox scan ({len(specs)} files)", "running")

    async def _run():
        try:
            summary = await ingest_batch(
                specs,
                no_upload=req.no_upload,
                auto_create=req.auto_create,
                fast=req.fast,
                progress_callback=cb,
                async_input_callback=input_cb,
                archive_inbox=req.archive,
            )
            cb(f"Inbox เสร็จ — {summary['ok']} ok / {summary['failed']} failed", "done")
            processing_status[session_id] = "done"
        except Exception as e:
            cb("Inbox error", "error", str(e))
            processing_status[session_id] = f"error:{e}"

    asyncio.create_task(_run())
    return {"session_id": session_id, "total": len(specs), "processing": True}


@app.get("/info")
async def get_server_info():
    """Return public URL written by start_server.py."""
    url_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cf_url")
    public_url = ""
    if os.path.exists(url_file):
        public_url = open(url_file).read().strip()
    return {
        "public_url": public_url or None,
        "local_url": "http://localhost:8000",
        "ui": f"{public_url}/static/index.html" if public_url else None,
    }


@app.get("/progress/{session_id}")
async def get_progress(session_id: str):
    _cleanup_expired()
    # KB sessions aren't in 'sessions' dict, but they are in 'processing_status'
    if session_id not in sessions and session_id not in processing_status:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    job = processing_status.get(session_id, "done")
    return {
        "session_id": session_id,
        "steps": _step_log.get(session_id, []),
        "processing": job == "processing",
    }


@app.delete("/session/{session_id}")
async def delete_session(session_id: str):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    sessions.pop(session_id, None)
    last_active.pop(session_id, None)
    _step_log.pop(session_id, None)
    return {"deleted": session_id, "success": True}


# ── ExamFlow ingest endpoints ────────────────────────────────────────────────────

_EXAM_KB_PATH   = os.getenv("EXAM_KB_PATH", "./examflow/exam_kb.json")
_EXAM_INBOX_DIR = Path(os.getenv("EXAM_INBOX_PATH", "./inbox/exams/"))


@app.get("/exam_kb_status")
async def exam_kb_status():
    if not os.path.exists(_EXAM_KB_PATH):
        return {"exists": False, "total_questions": 0, "source_files": [],
                "generated_at": None, "top_diseases": []}
    kb = json.loads(Path(_EXAM_KB_PATH).read_text(encoding="utf-8"))
    return {
        "exists": True,
        "total_questions": kb["metadata"]["total_questions"],
        "total_notes":     kb["metadata"].get("total_notes", len(kb.get("notes", []))),
        "source_files":    kb["metadata"]["source_files"],
        "generated_at":    kb["metadata"]["generated_at"],
        "top_diseases":    kb["pattern_analysis"]["most_frequent_diseases"][:5],
    }


@app.post("/ingest_exam")
async def ingest_exam_upload(
    file: UploadFile = File(...),
    year: Optional[int] = Form(None),
):
    session_id = f"exam_{str(uuid.uuid4())[:8]}"
    _step_log[session_id] = []
    processing_status[session_id] = "processing"
    cb = _make_progress_callback(session_id)

    _EXAM_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    dest = _EXAM_INBOX_DIR / file.filename
    with open(dest, "wb") as buf:
        shutil.copyfileobj(file.file, buf)
    cb(f"อัพโหลด {file.filename} สำเร็จ", "running")

    async def _run():
        try:
            count = await asyncio.to_thread(_ingest_exam_pdf, str(dest), year)
            cb(f"เสร็จ — เพิ่ม {count} ข้อ", "done")
            processing_status[session_id] = "done"
        except Exception as e:
            cb("เกิดข้อผิดพลาด", "error", str(e))
            processing_status[session_id] = f"error:{e}"

    asyncio.create_task(_run())
    return {"session_id": session_id, "processing": True}


@app.post("/dedup_exam")
async def dedup_exam():
    """Remove duplicate questions from exam_kb.json. Returns before/after counts."""
    from examflow.ingest_exam import dedup_kb
    try:
        before, after = await asyncio.to_thread(dedup_kb)
        return {"before": before, "after": after, "removed": before - after}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/scan_exam_inbox")
async def scan_exam_inbox():
    pdfs = sorted(_EXAM_INBOX_DIR.glob("*.pdf")) if _EXAM_INBOX_DIR.exists() else []
    return {
        "inbox_dir": str(_EXAM_INBOX_DIR.resolve()),
        "count": len(pdfs),
        "files": [p.name for p in pdfs],
    }


@app.post("/ingest_exam_inbox")
async def ingest_exam_from_inbox():
    pdfs = sorted(_EXAM_INBOX_DIR.glob("*.pdf")) if _EXAM_INBOX_DIR.exists() else []
    if not pdfs:
        raise HTTPException(status_code=400,
                            detail=f"ไม่พบ PDF ใน {_EXAM_INBOX_DIR} — วาง PDF แล้วลองใหม่")

    session_id = f"exam_{str(uuid.uuid4())[:8]}"
    _step_log[session_id] = []
    processing_status[session_id] = "processing"
    cb = _make_progress_callback(session_id)
    cb(f"พบ {len(pdfs)} ไฟล์ใน inbox/exams/", "running")

    async def _run():
        total = 0
        for pdf in pdfs:
            cb(f"กำลัง ingest: {pdf.name}", "running")
            try:
                count = await asyncio.to_thread(_ingest_exam_pdf, str(pdf))
                total += count
                cb(f"{pdf.name}: +{count} ข้อ", "done")
            except Exception as e:
                cb(f"{pdf.name}: ผิดพลาด", "error", str(e))
        cb(f"เสร็จทั้งหมด — รวม +{total} ข้อใหม่", "done")
        processing_status[session_id] = "done"

    asyncio.create_task(_run())
    return {"session_id": session_id, "total_files": len(pdfs), "processing": True}


# ── Lecture Bridge (G8) ───────────────────────────────────────────────────────

_LECTURE_INBOX_DIR = Path("./inbox/lectures")


@app.post("/lecture_bridge")
async def lecture_bridge_upload(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    hints: Optional[str] = Form(None),      # JSON array string: '["hint1","hint2"]'
    no_cache: bool = Form(False),
    no_g7: bool = Form(False),
):
    """Upload a lecture PDF and run G8 Lecture-Exam Bridge."""
    session_id = f"lecture_{str(uuid.uuid4())[:8]}"
    _step_log[session_id] = []
    processing_status[session_id] = "processing"
    cb = _make_progress_callback(session_id)

    _LECTURE_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    dest = _LECTURE_INBOX_DIR / file.filename
    with open(dest, "wb") as buf:
        shutil.copyfileobj(file.file, buf)
    cb(f"อัพโหลด {file.filename} สำเร็จ", "running")

    hints_list: list[str] = []
    if hints:
        try:
            hints_list = json.loads(hints)
        except Exception:
            hints_list = [h.strip() for h in hints.split("\n") if h.strip()]

    async def _run():
        try:
            from examflow.lecture_bridge import run_lecture_bridge

            def _progress_bridge(msg: str):
                cb(msg, "running")

            import sys, io
            # capture print() output and relay as progress steps
            old_stdout = sys.stdout
            class _StepWriter(io.TextIOBase):
                def write(self, s):
                    s = s.strip()
                    if s:
                        cb(s, "running")
                    return len(s)
                def flush(self): pass
            sys.stdout = _StepWriter()
            try:
                result = await asyncio.to_thread(
                    run_lecture_bridge,
                    str(dest),
                    title_override=title or "",
                    hints=hints_list or None,
                    use_cache=not no_cache,
                    auto_g7=not no_g7,
                )
            finally:
                sys.stdout = old_stdout

            cb("เสร็จแล้ว — รายงานบันทึกใน reports/ และ Obsidian แล้ว", "done")
            processing_status[session_id] = "done"
            # store result for retrieval
            processing_status[f"{session_id}_result"] = result
        except Exception as e:
            cb(f"เกิดข้อผิดพลาด: {e}", "error", str(e))
            processing_status[session_id] = f"error:{e}"

    asyncio.create_task(_run())
    return {"session_id": session_id, "processing": True}


@app.get("/lecture_bridge_result/{session_id}")
async def lecture_bridge_result(session_id: str):
    result = processing_status.get(f"{session_id}_result")
    if result is None:
        raise HTTPException(status_code=404, detail="Result not ready or session not found")
    return {"result": result}


# ── AI Agent endpoint (Claude with tool use) ──────────────────────────────────

_claude_client = _anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
_agent_histories: dict[str, list] = {}   # chat_id → [{role, content}, ...]
_agent_status:   dict[str, str]   = {}   # chat_id → "processing" | "done" | "error:..."
_agent_results:  dict[str, dict]  = {}   # chat_id → {reply, tools_used}
_agent_progress: dict[str, list]  = {}   # chat_id → [{label, status, detail}]

TOOL_LABELS_SERVER = {
    "get_kb_summary":        {"icon": "📊", "label": "อ่าน Exam KB"},
    "get_disease_info":      {"icon": "🔍", "label": "ดึงข้อมูลโรค"},
    "run_examflow_pipeline": {"icon": "🎓", "label": "ExamFlow pipeline"},
    "search_medical_kb":     {"icon": "📚", "label": "ค้น Medical KB"},
    "run_clinical_pipeline": {"icon": "🏥", "label": "Clinical pipeline"},
    "build_vault_note":      {"icon": "📝", "label": "สร้าง Vault note"},
    "save_to_vault":         {"icon": "💾", "label": "บันทึกลง Obsidian"},
    "call_agent":            {"icon": "🤖", "label": "เรียก agent"},
    "run_python":            {"icon": "🐍", "label": "รัน Python"},
}

_AGENT_SYSTEM = """You are an AI medical orchestrator inside CaseFlow — built for Thai Year-4 medical students.

## Core behavior
- **Always produce structured reports** — never give short bullet-point answers alone
- **Chain multiple tools** for comprehensive output: e.g. get_disease_info → run_examflow_pipeline → cross-reference
- Think like a senior doctor writing a teaching document, not a chatbot
- **"เช่น X" or "for example X"** in user input = EXAMPLE of what they want, NOT a disease to look up. Always address the FULL request first, not just the example.
- **Abbreviations**: ACS=Myocardial infarction, SLE, DM, HTN, PE, DVT, COPD, CAP — use get_disease_info with full name

## Workflow
1. Understand the request
2. Call relevant tools to gather data (call multiple if needed)
3. Synthesize into a well-structured report with sections
4. Cite sources: "[จาก exam_kb: Q2026_01]", "[จาก pipeline: G3]", "[จาก medical KB]"

## Tool use strategy

### Exam / study questions
- **ออกอะไรบ่อย / เก็ง / pattern** → get_kb_summary → run_examflow_pipeline(G2)
- **สรุปโรค X สำหรับสอบ** → get_disease_info(X) → run_examflow_pipeline(G3, X)
- **สอบพรุ่งนี้ X** → get_disease_info(X) → run_examflow_pipeline(G6, X)
- **ต้องอ่านอะไร / scope** → get_kb_summary → run_examflow_pipeline(G1)
- **ออกโจทย์ X** → get_disease_info(X) → run_examflow_pipeline(G4, X)
- **ยังขาดอะไร** → run_examflow_pipeline(G5)

### Clinical work
- **ข้อมูลผู้ป่วย / case analysis** → run_clinical_pipeline(A, patient_data)
- **เขียน progress note / SOAP** → run_clinical_pipeline(D, note_data)
- **แปล lab/EKG/ABG** → run_clinical_pipeline(F, lab_data)
- **เขียนใบขาว** → run_clinical_pipeline(G, patient_data)
- **เตรียม morning round** → run_clinical_pipeline(E, case_summary)
- **ถามความรู้ / guideline** → search_medical_kb → run_clinical_pipeline(B, query)
- **มีอาการนำ ยังไม่มี lab** → run_clinical_pipeline(C, symptoms)

### Notes / saving
- **สร้าง vault note เรื่อง X** → build_vault_note(X, diseases/drugs/approaches/labs/procedures)
- **สร้าง note pool / หลาย note** → run_python: loop สร้าง note ทีละอัน แล้ว save_to_vault
- **note pool คำศัพท์ / terminology / X-ray findings** → run_python: สร้าง consolidated note ที่มีทุก term ในไฟล์เดียว แล้ว save_to_vault
- **บันทึก report → Obsidian** → save_to_vault(content, filename)

### Note pool ด้วย run_python
```python
# สร้าง note pool X-ray findings ตัวอย่าง:
content = call_agent('kb_retrieval', prompt='X-ray findings vocabulary: consolidation, pleural effusion, pneumothorax, ground glass opacity, etc. Definition, appearance, causes for each')
analysis = call_agent('analyzer', prompt=f'สร้าง Obsidian note เรื่อง X-ray Findings Vocabulary รวม findings ทั้งหมด\\n{content}')
print(call_agent('formatter', prompt=analysis))
# แล้ว save ด้วย save_to_vault tool
```

### run_python — ทรงพลังที่สุด ใช้เมื่อ tool อื่นไม่พอ
ใช้ `run_python` เพื่อ chain agents เองแบบ custom pipeline หรือทำสิ่งที่ tool อื่นทำไม่ได้:

```python
# เรียก agent ใดก็ได้ใน AGENT_MODELS:
result = call_agent('analyzer', prompt='ผู้ป่วย 45 ปี...', system=load_prompt('analyzer'))
print(result)

# chain หลาย agents:
sources = call_agent('kb_retrieval', prompt='HIV management guidelines')
analysis = call_agent('analyzer', prompt=f'Sources: {sources}\\n\\nPatient: ...')
print(call_agent('formatter', prompt=analysis))

# ExamFlow branches:
result = run_examflow_branch('G3', 'HIV infection')
print(result)

# ดู agents ที่มีทั้งหมด:
print(list(AGENT_MODELS.keys()))
```

**pre-imported ให้ทุก run:**
- `call_agent(name, prompt, system="")` — เรียก agent ใดก็ได้
- `load_prompt(name)` — โหลด system prompt ของ agent
- `run_examflow_branch(branch, query)` — G1-G6
- `AGENT_MODELS`, `AGENT_LABELS` — รายชื่อ agents ทั้งหมด
- `json`, `os`, `re`, `datetime`

### Custom pipelines with call_agent tool
Use `call_agent` tool (not run_python) for single-agent calls with instruction + context.

### Always chain tools:
Get data first → run pipeline/agents → optionally save result

## Output format
Always structure output as a proper report with:
- หัวข้อ / sections
- ตาราง where appropriate
- Sources at the bottom

**CRITICAL — reply vs save:**
- เมื่อ generate report แล้ว ต้อง **paste เนื้อหาเต็มลงใน reply นี้ก่อนเสมอ** อย่าตอบแค่ "บันทึกแล้ว"
- แล้วค่อย save_to_vault เป็น step สุดท้าย
- reply = เนื้อหาที่ user เห็นใน chat; vault = สำเนาที่บันทึกไว้

## Language
Respond in Thai. Use English for medical terms (disease names, drugs, investigations).
"""

# ── Tool definitions ──────────────────────────────────────────────────────────

_TOOLS = [
    {
        "name": "get_kb_summary",
        "description": "Get a summary of the exam knowledge base: top diseases by frequency, topic distribution, and trend analysis.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_disease_info",
        "description": "Get detailed exam data for a specific disease: frequency, years appeared, investigations, management, distractors, and exam notes/slides.",
        "input_schema": {
            "type": "object",
            "properties": {
                "disease_name": {"type": "string", "description": "Disease name (English preferred, e.g. 'HIV infection', 'Type 2 diabetes mellitus')"}
            },
            "required": ["disease_name"],
        },
    },
    {
        "name": "run_examflow_pipeline",
        "description": "Run a full ExamFlow pipeline to generate a formal report. Use for: scope/priority topics (G1), exam pattern analysis (G2), detailed disease summary (G3), practice vignette (G4), knowledge gap detection (G5), ultra-compact summary (G6).",
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "enum": ["G1", "G2", "G3", "G4", "G5", "G6"],
                    "description": "G1=scope, G2=pattern/เก็ง, G3=disease summary, G4=vignette, G5=gap, G6=ultra summary"
                },
                "query": {"type": "string", "description": "The user's question or topic to analyze"}
            },
            "required": ["branch", "query"],
        },
    },
    {
        "name": "search_medical_kb",
        "description": "Search the medical knowledge base (textbooks, guidelines, slides) for clinical information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "specialty": {"type": "string", "description": "Optional specialty filter (e.g. 'Cardiology', 'Pulmonology')"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_clinical_pipeline",
        "description": (
            "Run a CaseFlow clinical pipeline branch and return the full result. "
            "Use for patient case analysis, writing clinical notes, interpreting labs/EKG, etc.\n"
            "Branches:\n"
            "  A = Full DDx case analysis (CC + vitals + labs)\n"
            "  B = Knowledge query (mechanism, guideline, pharmacology)\n"
            "  C = Symptom approach (symptom only, no objective data)\n"
            "  D = Progress note / SOAP\n"
            "  E = Morning round preparation\n"
            "  F = Lab / ABG / EKG / imaging interpretation\n"
            "  G = Admission note (ใบขาว)\n"
            "  H = Note blind spot checker (ตรวจจุดบอดใบเหลือง — ควรซักอะไรเพิ่ม)\n"
            "  N = Translate/explain raw clinical notes"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "enum": ["A","B","C","D","E","F","G","H","N"],
                    "description": "Pipeline branch to run"
                },
                "input_text": {
                    "type": "string",
                    "description": "Patient data, clinical note, or query to process"
                }
            },
            "required": ["branch", "input_text"],
        },
    },
    {
        "name": "build_vault_note",
        "description": "Build an Obsidian vault note for a medical topic using the vault_builder pipeline. Saves to Obsidian automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic":      {"type": "string", "description": "Topic name (e.g. 'HIV infection', 'Metformin')"},
                "topic_type": {"type": "string", "enum": ["diseases","drugs","approaches","labs","procedures"], "description": "Category of the topic"}
            },
            "required": ["topic", "topic_type"],
        },
    },
    {
        "name": "save_to_vault",
        "description": "Save a markdown document to the Obsidian vault inbox. Use after generating a report to persist it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content":  {"type": "string", "description": "Markdown content to save"},
                "filename": {"type": "string", "description": "Filename without .md extension (e.g. 'HIV_Summary_2567')"}
            },
            "required": ["content", "filename"],
        },
    },
    {
        "name": "call_agent",
        "description": (
            "Call a single CaseFlow/ExamFlow agent directly with custom input. "
            "Use this to build custom pipelines: chain agents in any order you decide.\n\n"
            "Clinical agents:\n"
            "  kb_retrieval  — search RAG knowledge base, returns relevant medical passages\n"
            "  researcher    — web-grounded research, finds guidelines and evidence\n"
            "  analyzer      — clinical reasoning, DDx, assessment\n"
            "  challenger    — critiques and stress-tests a clinical analysis\n"
            "  professor     — explains/teaches the final answer clearly\n"
            "  drug_agent    — drug dosing, interactions, renal/hepatic adjustment\n"
            "  interpreter   — interprets labs, ABG, EKG, imaging findings\n"
            "  patho_agent   — pathophysiology and mechanism explanation\n"
            "  score_agent   — calculates clinical scores (SOFA, CURB-65, CHA2DS2, etc.)\n"
            "  formatter     — formats final output as clean structured markdown\n\n"
            "ExamFlow agents (use exam_kb.json):\n"
            "  examflow_scope    — G1: study priority\n"
            "  examflow_analysis — G2: exam pattern\n"
            "  examflow_disease  — G3: disease summary\n"
            "  examflow_vignette — G4: practice MCQ\n"
            "  examflow_gap      — G5: knowledge gaps\n"
            "  examflow_ultra    — G6: quick summary\n"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": [
                        "kb_retrieval","researcher","analyzer","challenger","professor",
                        "drug_agent","interpreter","patho_agent","score_agent","formatter",
                        "examflow_scope","examflow_analysis","examflow_disease",
                        "examflow_vignette","examflow_gap","examflow_ultra",
                    ],
                    "description": "Agent to call"
                },
                "instruction": {
                    "type": "string",
                    "description": "What you want this agent to do (task description)"
                },
                "context": {
                    "type": "string",
                    "description": "Input data/context for the agent — can be patient data, previous agent output, KB content, etc."
                }
            },
            "required": ["agent", "instruction", "context"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Execute Python code directly in the CaseFlow environment — like Claude CLI's bash tool.\n"
            "Use this when other tools don't cover your needs: custom agent chains, complex logic, "
            "reading files, or calling any agent with custom parameters.\n\n"
            "Pre-imported for every run:\n"
            "  call_agent(name, prompt, system='')  — call any agent in AGENT_MODELS\n"
            "  load_prompt(name)                    — load agent system prompt\n"
            "  run_examflow_branch(branch, query)   — G1–G6\n"
            "  AGENT_MODELS, AGENT_LABELS           — all available agents\n"
            "  json, os, re, datetime\n\n"
            "IMPORTANT: always print() output you want to see. stdout is captured as result.\n\n"
            "Example — chain analyzer + formatter:\n"
            "  result = call_agent('analyzer', prompt='ผู้ป่วย 45 ปี...')\n"
            "  print(call_agent('formatter', prompt=result))\n\n"
            "Example — ExamFlow G3:\n"
            "  print(run_examflow_branch('G3', 'HIV infection'))"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to run. Must print() any output you want captured."
                }
            },
            "required": ["code"],
        },
    },
]


# ── Tool execution ─────────────────────────────────────────────────────────────

def _tool_get_kb_summary(_input: dict) -> str:
    kb_path = os.getenv("EXAM_KB_PATH", "./examflow/exam_kb.json")
    if not os.path.exists(kb_path):
        return "exam_kb ยังไม่มีข้อมูล — กรุณา ingest ข้อสอบก่อน"
    try:
        kb = json.loads(Path(kb_path).read_text(encoding="utf-8"))
        total_q = kb["metadata"]["total_questions"]
        total_n = kb["metadata"].get("total_notes", 0)
        top_dis = kb["pattern_analysis"]["most_frequent_diseases"][:10]
        dist    = kb["pattern_analysis"]["topic_type_distribution"]
        lines   = [f"Total: {total_q} questions + {total_n} notes\n\nTop diseases:"]
        for d in top_dis:
            e = kb["disease_index"].get(d, {})
            lines.append(f"  {d}: {e.get('frequency',0)}x | trend={e.get('trend','?')}")
        lines.append(f"\nTopic dist: {dist}")
        predicted = kb["pattern_analysis"].get("predicted_high_yield_next", [])
        if predicted:
            lines.append(f"Predicted high-yield: {predicted}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading KB: {e}"


_ABBREV_MAP = {
    "acs": "myocardial infarction",
    "stemi": "myocardial infarction",
    "nstemi": "myocardial infarction",
    "mi": "myocardial infarction",
    "chf": "heart failure",
    "hf": "heart failure",
    "hfref": "heart failure with reduced ejection fraction",
    "copd": "chronic obstructive pulmonary disease",
    "dm": "type 2 diabetes mellitus",
    "dm2": "type 2 diabetes mellitus",
    "t2dm": "type 2 diabetes mellitus",
    "dka": "diabetic ketoacidosis",
    "ht": "hypertension",
    "htn": "hypertension",
    "cva": "stroke",
    "tia": "transient ischemic attack",
    "pe": "pulmonary embolism",
    "dvt": "deep vein thrombosis",
    "vte": "venous thromboembolism",
    "sle": "systemic lupus erythematosus",
    "ra": "rheumatoid arthritis",
    "aki": "acute kidney injury",
    "ckd": "chronic kidney disease",
    "esrd": "end-stage renal disease",
    "tb": "tuberculosis",
    "tbm": "tuberculous meningitis",
    "cm": "cryptococcal meningitis",
    "hiv": "hiv infection",
    "aids": "hiv infection",
    "oi": "hiv infection",
    "gi": "gastrointestinal bleeding",
    "gerd": "gastroesophageal reflux disease",
    "aml": "leukemia",
    "cml": "leukemia",
    "itp": "thrombocytopenia",
    "ttp": "thrombotic thrombocytopenic purpura",
    "hus": "hemolytic uremic syndrome",
    "aiha": "autoimmune hemolytic anemia",
    "svt": "supraventricular tachycardia",
    "af": "atrial fibrillation",
    "ms": "mitral stenosis",
    "mr": "mitral regurgitation",
    "as": "aortic stenosis",
    "cap": "pneumonia",
    "hap": "pneumonia",
    "siadh": "hyponatremia",
}

def _tool_get_disease_info(inp: dict) -> str:
    kb_path = os.getenv("EXAM_KB_PATH", "./examflow/exam_kb.json")
    if not os.path.exists(kb_path):
        return "exam_kb ไม่มีข้อมูล"
    try:
        kb    = json.loads(Path(kb_path).read_text(encoding="utf-8"))
        name  = inp.get("disease_name", "")
        name_lower = name.lower().strip()
        # resolve abbreviation
        name_lower = _ABBREV_MAP.get(name_lower, name_lower)
        # fuzzy match
        match_key = next(
            (k for k in kb["disease_index"] if name_lower in k.lower() or k.lower() in name_lower),
            None
        )
        if not match_key:
            known = list(kb["disease_index"].keys())[:20]
            return f"ไม่พบ '{name}' ใน KB\nโรคที่มี: {known}"
        entry = kb["disease_index"][match_key]
        # get related questions
        q_ids = entry.get("question_ids", [])[:5]
        questions = [q for q in kb["questions"] if q["id"] in set(q_ids)]
        # get related notes
        n_ids = entry.get("note_ids", [])[:3]
        notes = [n for n in kb.get("notes", []) if n["id"] in set(n_ids)]

        out = [f"Disease: {match_key}",
               f"Frequency: {entry.get('frequency',0)}x | Years: {entry.get('years_appeared',[])} | Trend: {entry.get('trend','?')}",
               f"Key investigations: {entry.get('key_investigations',[])}",
               f"Key management: {entry.get('key_management',[])}",
               f"Must-know facts: {entry.get('must_know_facts',[])[:5]}",
               f"Common distractors: {entry.get('common_distractors',[])}",
               ""]
        for q in questions:
            out.append(f"Q [{q['id']}]: {q.get('question_text','')[:200]}")
            out.append(f"  Answer: {q.get('answer_key')} | Type: {q.get('topic_type')} | Diff: {q.get('difficulty')}\n")
        for n in notes:
            out.append(f"Note [{n['id']}] ({n.get('note_type','?')}): {n.get('content','')[:300]}\n")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


def _tool_run_examflow_pipeline(inp: dict) -> str:
    branch = inp.get("branch", "G3")
    query  = inp.get("query", "")
    try:
        from examflow.pipeline import run_examflow_branch
        result = run_examflow_branch(branch, query)
        # truncate for Claude context (keep full in session)
        return result[:8000] + ("\n\n[truncated — full report saved to reports/]" if len(result) > 8000 else "")
    except Exception as e:
        return f"Pipeline error: {e}"


def _tool_search_medical_kb(inp: dict) -> str:
    try:
        import knowledge_rag as rag
        specialty = inp.get("specialty") or "general"
        result = rag.query(specialty, inp.get("query", ""), n_results=3)
        if not result or result.startswith("[RAG:"):
            return "ไม่พบข้อมูลใน medical KB"
        return result[:1500]
    except Exception as e:
        return f"KB search error: {e}"




def _tool_run_clinical_pipeline(inp: dict) -> str:
    """Run a CaseFlow clinical branch directly (no web session / no confirm)."""
    branch     = inp.get("branch", "A").upper()
    input_text = inp.get("input_text", "")
    try:
        from session import CaseSession, detect_branch
        import uuid as _uuid
        sess = CaseSession(session_id=_uuid.uuid4().hex[:8])
        sess.patient_data = input_text
        sess.branch       = branch

        from session import _EXAMFLOW_BRANCH_MAP
        if branch in _EXAMFLOW_BRANCH_MAP:
            from examflow.pipeline import run_examflow_branch
            result = run_examflow_branch(_EXAMFLOW_BRANCH_MAP[branch], input_text)
        else:
            result = sess._run_pipeline(input_text, [])

        sess.draft = result
        sess.save_state()
        return result[:8000] + ("\n\n[truncated]" if len(result) > 8000 else "")
    except Exception as e:
        return f"Clinical pipeline error: {e}"


def _tool_build_vault_note(inp: dict) -> str:
    """Build one vault note via vault_builder."""
    topic      = inp.get("topic", "")
    topic_type = inp.get("topic_type", "diseases")
    if not topic:
        return "Error: topic is required"
    try:
        from vault_builder import generate_note
        content = generate_note(topic, topic_type)
        if not content:
            return f"vault_builder ไม่สามารถสร้าง note สำหรับ '{topic}' ได้"

        vault_root = os.getenv("OBSIDIAN_VAULT_PATH", r"C:\Users\ASUS\Documents\Obsidian vault")
        folder_map = {
            "diseases":   "02 - Diseases",
            "drugs":      "03 - Drugs",
            "approaches": "08 - Approaches",
            "labs":       "04 - Labs",
            "procedures": "07 - Procedures",
        }
        folder   = os.path.join(vault_root, folder_map.get(topic_type, topic_type.capitalize()))
        os.makedirs(folder, exist_ok=True)
        filename = topic.replace(" ", "_") + ".md"
        filepath = os.path.join(folder, filename)
        Path(filepath).write_text(content, encoding="utf-8")
        return f"✅ สร้าง note '{topic}' แล้ว\nบันทึกที่: {filepath}\n\n{content[:500]}..."
    except Exception as e:
        return f"vault_builder error: {e}"


def _tool_save_to_vault(inp: dict) -> str:
    """Save markdown content to Obsidian vault inbox."""
    content  = inp.get("content", "")
    filename = inp.get("filename", "caseflow_note")
    if not content:
        return "Error: content is required"
    try:
        vault_root = os.getenv("OBSIDIAN_VAULT_PATH", r"C:\Users\ASUS\Documents\Obsidian vault")
        inbox      = os.path.join(vault_root, "00 - Inbox")
        os.makedirs(inbox, exist_ok=True)
        # sanitize filename
        import re as _re
        safe_name = _re.sub(r'[<>:"/\\|?*]', '_', filename).strip()
        if not safe_name.endswith(".md"):
            safe_name += ".md"
        filepath = os.path.join(inbox, safe_name)
        Path(filepath).write_text(content, encoding="utf-8")
        return f"✅ บันทึกแล้ว: 00 - Inbox/{safe_name}"
    except Exception as e:
        return f"Save error: {e}"


def _tool_run_python(inp: dict) -> str:
    """Execute Python code with CaseFlow environment pre-loaded."""
    code = inp.get("code", "").strip()
    if not code:
        return "Error: no code provided"

    import subprocess, sys, tempfile

    _CF_DIR = os.path.dirname(os.path.abspath(__file__))
    _venv_py = os.path.join(_CF_DIR, ".venv", "Scripts", "python.exe")
    _py_exe  = _venv_py if os.path.exists(_venv_py) else sys.executable

    preamble = (
        f'import sys, os\n'
        f'sys.path.insert(0, r"{_CF_DIR}")\n'
        f'os.chdir(r"{_CF_DIR}")\n'
        f'from dotenv import load_dotenv; load_dotenv()\n'
        f'from router import call_agent, load_prompt, AGENT_MODELS, AGENT_LABELS\n'
        f'from examflow.pipeline import run_examflow_branch\n'
        f'import json, re, datetime\n\n'
    )

    full_code = preamble + code

    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.py', delete=False,
        encoding='utf-8', dir=_CF_DIR
    ) as f:
        f.write(full_code)
        tmpfile = f.name

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        proc = subprocess.run(
            [_py_exe, tmpfile],
            capture_output=True, text=True, timeout=180,
            cwd=_CF_DIR, env=env, encoding="utf-8",
        )
        output = proc.stdout.strip()
        if proc.returncode != 0 and proc.stderr:
            stderr = proc.stderr.strip()[:600]
            output = (output + f"\n[stderr]: {stderr}") if output else f"Error (exit {proc.returncode}): {stderr}"
        return output[:6000] + ("\n\n[output truncated]" if len(output) > 6000 else "")
    except subprocess.TimeoutExpired:
        return "Error: execution timeout (180s)"
    except Exception as e:
        return f"Error: {e}"
    finally:
        try:
            os.unlink(tmpfile)
        except Exception:
            pass


def _tool_call_agent(inp: dict) -> str:
    """Call any single CaseFlow agent directly."""
    agent_name  = inp.get("agent", "")
    instruction = inp.get("instruction", "")
    context     = inp.get("context", "")
    if not agent_name:
        return "Error: agent name required"
    try:
        from router import call_agent, load_prompt
        from session import _EXAMFLOW_BRANCH_MAP
        if agent_name in _EXAMFLOW_BRANCH_MAP:
            from examflow.pipeline import run_examflow_branch
            result = run_examflow_branch(_EXAMFLOW_BRANCH_MAP[agent_name], context or instruction)
        else:
            system = load_prompt(agent_name)
            result = call_agent(
                agent_name,
                system=system,
                prompt=f"Instruction: {instruction}\n\nContext:\n{context}",
            )
        return result[:8000] + ("\n[truncated]" if len(result) > 8000 else "")
    except Exception as e:
        return f"Agent '{agent_name}' error: {e}"


_TOOL_HANDLERS = {
    "get_kb_summary":          _tool_get_kb_summary,
    "get_disease_info":        _tool_get_disease_info,
    "run_examflow_pipeline":   _tool_run_examflow_pipeline,
    "search_medical_kb":       _tool_search_medical_kb,
    "run_clinical_pipeline":   _tool_run_clinical_pipeline,
    "build_vault_note":        _tool_build_vault_note,
    "save_to_vault":           _tool_save_to_vault,
    "call_agent":              _tool_call_agent,
    "run_python":              _tool_run_python,
}


# ── Agent loop ─────────────────────────────────────────────────────────────────

class AgentRequest(BaseModel):
    message: str
    chat_id: str = "default"


def _run_agent_sync(chat_id: str, messages: list, history: list) -> None:
    """Blocking agent loop — runs in a thread pool via asyncio.to_thread."""
    tools_used: list = []
    MAX_ROUNDS = 10
    reply = ""
    prog = _agent_progress.setdefault(chat_id, [])
    prog.clear()

    def _log(label: str, status: str, detail: str = ""):
        if prog and prog[-1]["label"] == label and prog[-1]["status"] == "running":
            prog[-1]["status"] = status
            if detail:
                prog[-1]["detail"] = detail
        else:
            prog.append({"label": label, "status": status, "detail": detail})

    _log("Claude วิเคราะห์คำถาม", "running")

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _loop():
            nonlocal reply, tools_used
            for _ in range(MAX_ROUNDS):
                try:
                    resp = await asyncio.to_thread(
                        lambda m=messages: _claude_client.messages.create(
                            model="claude-sonnet-5",
                            max_tokens=16384,
                            thinking={"type": "adaptive"},
                            system=_AGENT_SYSTEM,
                            tools=_TOOLS,
                            messages=m,
                        )
                    )
                except Exception as e:
                    reply = f"❌ Claude API error: {str(e)[:300]}"
                    _log("Claude วิเคราะห์คำถาม", "error", str(e)[:60])
                    break

                text_parts = [b.text for b in resp.content if b.type == "text" and b.text]

                if resp.stop_reason == "end_turn":
                    _log("Claude วิเคราะห์คำถาม", "done")
                    _log("สรุปผลลัพธ์", "running")
                    reply = "\n".join(text_parts) or "(ไม่มีคำตอบ)"
                    _log("สรุปผลลัพธ์", "done")
                    break

                if resp.stop_reason == "tool_use":
                    _log("Claude วิเคราะห์คำถาม", "done")
                    messages.append({"role": "assistant", "content": resp.content})
                    tool_results = []
                    for block in resp.content:
                        if block.type != "tool_use":
                            continue
                        info = TOOL_LABELS_SERVER.get(block.name, {"icon": "🔧", "label": block.name})
                        step_label = f"{info['icon']} {info['label']}"
                        # ใส่ detail จาก input (เช่น ชื่อโรค/query)
                        inp_vals = [str(v) for v in block.input.values() if isinstance(v, str)]
                        detail = ", ".join(inp_vals)[:60] if inp_vals else ""
                        _log(step_label, "running", detail)

                        handler = _TOOL_HANDLERS.get(block.name)
                        tools_used.append({"tool": block.name, "input": block.input})
                        if handler:
                            try:
                                result_text = await asyncio.to_thread(handler, block.input)
                                _log(step_label, "done")
                            except Exception as te:
                                result_text = f"Tool '{block.name}' error: {te}"
                                _log(step_label, "error", str(te)[:60])
                        else:
                            result_text = f"Unknown tool: {block.name}"
                            _log(step_label, "error", "unknown tool")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result_text) if result_text is not None else "(no result)",
                        })
                    _log("Claude วิเคราะห์คำถาม", "running")
                    messages.append({"role": "user", "content": tool_results})
                else:
                    reply = "\n".join(text_parts) or "(หยุดทำงานโดยไม่คาดคิด)"
                    break

        loop.run_until_complete(_loop())
        loop.close()
    except Exception as outer_e:
        reply = f"❌ Agent error: {outer_e}"

    history.append({"role": "assistant", "content": reply or "(ไม่มีคำตอบ)"})
    _agent_histories[chat_id] = history[-40:]
    _agent_results[chat_id] = {"reply": reply or "(ไม่มีคำตอบ)", "tools_used": tools_used}
    _agent_status[chat_id] = "done"


@app.post("/agent")
async def agent_chat(req: AgentRequest):
    """Start agent processing — returns immediately; poll /agent_result/{chat_id}."""
    history = _agent_histories.setdefault(req.chat_id, [])
    history.append({"role": "user", "content": req.message})
    messages = list(history[-20:])

    _agent_status[req.chat_id] = "processing"
    _agent_results.pop(req.chat_id, None)

    asyncio.create_task(
        asyncio.to_thread(_run_agent_sync, req.chat_id, messages, history)
    )
    return {"chat_id": req.chat_id, "processing": True}


@app.get("/agent_result/{chat_id}")
async def agent_result(chat_id: str):
    """Poll for agent result after POST /agent."""
    status = _agent_status.get(chat_id, "unknown")
    progress = _agent_progress.get(chat_id, [])
    if status == "processing":
        return {"chat_id": chat_id, "processing": True, "progress": progress}
    result = _agent_results.get(chat_id, {})
    return {
        "chat_id": chat_id,
        "processing": False,
        "reply": result.get("reply", "(ไม่มีคำตอบ)"),
        "tools_used": result.get("tools_used", []),
        "progress": progress,
    }


@app.delete("/agent/{chat_id}")
async def clear_agent(chat_id: str):
    _agent_histories.pop(chat_id, None)
    _agent_status.pop(chat_id, None)
    _agent_results.pop(chat_id, None)
    return {"cleared": chat_id}


class QuickSaveRequest(BaseModel):
    content: str
    filename: str = ""

@app.post("/quick_save")
async def quick_save(req: QuickSaveRequest):
    """Save markdown content directly to Obsidian 00 - Inbox (no session needed)."""
    filename = req.filename or f"AgentReport_{datetime.now().strftime('%Y%m%d_%H%M')}"
    result = _tool_save_to_vault({"content": req.content, "filename": filename})
    return {"saved": result, "success": "Error" not in result}


# ── Vault Builder endpoints ───────────────────────────────────────────────────

_VB_STATUS_FILE = Path(os.path.dirname(os.path.abspath(__file__))) / ".vb_status.json"
_VAULT_ROOT     = Path(os.getenv("OBSIDIAN_VAULT_PATH", r"C:\Users\ASUS\Documents\Obsidian vault"))
_PYTHON_EXE     = r".venv\Scripts\python.exe"


class VaultBuildRequest(BaseModel):
    topics: str                   # comma/newline-separated topic list
    topic_type: str = "diseases"  # diseases | drugs | labs | approaches | procedures


@app.post("/vault_build")
async def vault_build(req: VaultBuildRequest):
    """Run vault_builder for a list of topics → save to Obsidian vault."""
    import re as _re, tempfile as _tmp

    raw_topics = [t.strip() for t in _re.split(r"[,\n;]+", req.topics) if t.strip()]
    if not raw_topics:
        raise HTTPException(status_code=400, detail="ไม่พบ topics — กรอกชื่อโรค/ยาที่ต้องการ")

    topic_dict = {req.topic_type: raw_topics}

    # Write temp JSON for vault_builder --from-json
    tmp = _tmp.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8",
        dir=os.path.dirname(os.path.abspath(__file__))
    )
    json.dump(topic_dict, tmp, ensure_ascii=False, indent=2)
    tmp.close()

    session_id = f"vb_{str(uuid.uuid4())[:8]}"
    _step_log[session_id] = []
    processing_status[session_id] = "processing"
    cb = _make_progress_callback(session_id)
    cb(f"เตรียม build {len(raw_topics)} notes ({req.topic_type})", "running")

    async def _run():
        try:
            vault_dir = str(_VAULT_ROOT)
            cmd = [_PYTHON_EXE, "vault_builder.py",
                   "--from-json", tmp.name,
                   "--output", vault_dir]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    status = "error" if "error" in line.lower() or "❌" in line else "running"
                    cb(line[:120], status)
            await proc.wait()
            os.unlink(tmp.name)
            if proc.returncode == 0:
                cb(f"เสร็จ — {len(raw_topics)} notes บันทึกลง Obsidian แล้ว", "done")
                processing_status[session_id] = "done"
            else:
                cb("vault_builder ออกด้วย error", "error")
                processing_status[session_id] = "error:vault_builder failed"
        except Exception as e:
            cb(f"Error: {e}", "error")
            processing_status[session_id] = f"error:{e}"

    asyncio.create_task(_run())
    return {"session_id": session_id, "topics": raw_topics, "processing": True}


@app.get("/vault_status")
async def vault_status():
    """Return current vault_builder progress from .vb_status.json."""
    if not _VB_STATUS_FILE.exists():
        return {"running": False, "finished": True}
    try:
        data = json.loads(_VB_STATUS_FILE.read_text(encoding="utf-8"))
        return data
    except Exception:
        return {"running": False, "finished": True}


@app.get("/vault_structure")
async def vault_structure():
    """List top-level folders + note counts in Obsidian vault."""
    if not _VAULT_ROOT.exists():
        return {"folders": [], "vault_path": str(_VAULT_ROOT), "exists": False}
    folders = []
    for item in sorted(_VAULT_ROOT.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            count = sum(1 for f in item.rglob("*.md"))
            folders.append({"name": item.name, "notes": count})
    return {"folders": folders, "vault_path": str(_VAULT_ROOT), "exists": True}


# Mount static files last to avoid route conflicts
app.mount("/static", StaticFiles(directory="static"), name="static")
