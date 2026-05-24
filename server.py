import asyncio
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

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
        "G": "Admission Note", "U": "Freestyle",
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


# Mount static files last to avoid route conflicts
app.mount("/static", StaticFiles(directory="static"), name="static")
