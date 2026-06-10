# Overnight batch — re-ingest + quiz generation
# รันใน terminal: powershell -ExecutionPolicy Bypass -File run_overnight.ps1

# Restart Ollama ให้รับ thread count ใหม่
Write-Host "Restarting Ollama..." -ForegroundColor Yellow
Stop-Process -Name "ollama" -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
Start-Sleep -Seconds 3
Write-Host "Ollama ready"
$env:OLLAMA_EXTRACTION_MODEL = "qwen2.5:14b"
$env:OLLAMA_QUIZ_MODEL = "qwen2.5:14b"

Set-Location "C:\Users\ASUS\Documents\caseflow"

Write-Host ""
Write-Host "=== STEP 1: Re-ingest (SKIPPED — using existing exam_kb.json) ===" -ForegroundColor DarkGray
# python examflow/ingest_exam.py --rerun --local

Write-Host ""
Write-Host "=== STEP 2: Generate quiz — scope all tiers, 3 questions each ===" -ForegroundColor Cyan
Write-Host "Started: $(Get-Date -Format 'HH:mm:ss')"
python quiz/generate_quiz.py --mode scope --n 3
Write-Host "Quiz done: $(Get-Date -Format 'HH:mm:ss')"

Write-Host ""
Write-Host "=== STEP 3: Update vault notes via G3 pipeline ===" -ForegroundColor Cyan
Write-Host "Started: $(Get-Date -Format 'HH:mm:ss')"
# ใช้ G3 (Gemini) — grounded ใน exam_kb, มี Grounding Gate
# fallback: python quiz/update_notes.py --min-coverage 80 (ใช้ Ollama ถ้า Gemini quota หมด)
powershell -ExecutionPolicy Bypass -File run_g3_batch.ps1
Write-Host "Notes updated: $(Get-Date -Format 'HH:mm:ss')"

Write-Host ""
Write-Host "=== ALL DONE ===" -ForegroundColor Green
Write-Host "Finished: $(Get-Date -Format 'HH:mm:ss')"
