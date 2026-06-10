# Batch G3 — สร้าง/อัพเดท disease notes ผ่าน pipeline สำหรับโรคที่ขาดเนื้อหา
# รัน: powershell -ExecutionPolicy Bypass -File run_g3_batch.ps1
# ใช้ Gemini API (ต้องการ quota) — ดีกว่า update_notes.py (Ollama) เพราะ grounded ใน exam_kb

Set-Location "C:\Users\ASUS\Documents\caseflow"

# Must-know diseases ที่ note ยังขาดเนื้อหา (< 80% coverage)
$diseases = @(
    "Hypothyroidism",
    "Hypokalemia",
    "Meningitis",
    "Dermatophytosis",
    "Melioidosis",
    "Stroke",
    "HIV infection",
    "Malaria",
    "Shigellosis",
    "Pneumonia",
    "Pleural effusion",
    "Dyslipidemia",
    "G6PD deficiency",
    "Vitamin B12 deficiency",
    "Autoimmune hemolytic anemia",
    "Systemic lupus erythematosus",
    "Pulmonary embolism",
    "Herpes simplex virus infection",
    "Catheter-related bloodstream infection",
    "Opioid overdose",
    "Aortic dissection"
)

$total = $diseases.Count
$done  = 0
$fail  = 0

Write-Host "=== G3 Batch — $total diseases ===" -ForegroundColor Cyan
Write-Host "Started: $(Get-Date -Format 'HH:mm:ss')"
Write-Host ""

foreach ($d in $diseases) {
    $done++
    Write-Host "[$done/$total] G3: $d" -ForegroundColor Yellow
    $start = Get-Date

    python examflow/query.py "[branch G3] สรุปโรค $d สำหรับข้อสอบ MCQ MEQ"
    $exit_code = $LASTEXITCODE

    $elapsed = [int]((Get-Date) - $start).TotalSeconds
    if ($exit_code -eq 0) {
        Write-Host "  -> OK ($elapsed s)" -ForegroundColor Green
    } else {
        Write-Host "  -> ERROR exit=$exit_code ($elapsed s)" -ForegroundColor Red
        $fail++
        # หยุดถ้า quota หมด (429)
        if ($exit_code -eq 1) {
            Write-Host "  (อาจเป็น quota exhausted — หยุด)" -ForegroundColor Red
            break
        }
    }
    Write-Host ""
    Start-Sleep -Seconds 2
}

Write-Host "=== Done: $($done - $fail) OK, $fail errors ===" -ForegroundColor Cyan
Write-Host "Finished: $(Get-Date -Format 'HH:mm:ss')"
