# AutoTube AI installer for Windows (PowerShell). Run:  powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
Write-Host "== AutoTube AI setup =="

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Host "Python 3.10+ is required: https://www.python.org/downloads/ (tick 'Add to PATH')"; exit 1 }
& python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { Write-Host "Python 3.10 or newer is required."; exit 1 }

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Host "FFmpeg not found. Install with:  winget install Gyan.FFmpeg   (then reopen this window)"
}
if (-not (Test-Path .venv)) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m pip install piper-tts
if ($LASTEXITCODE -ne 0) { Write-Host "(piper-tts could not be installed; Windows will fall back to the built-in SAPI voice)" }
& .\.venv\Scripts\python.exe main.py init

Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. AI model: .\.venv\Scripts\python.exe main.py set-key GROQ_API_KEY ; then main.py use-provider groq"
Write-Host "     or install Ollama (https://ollama.com) and run: ollama pull llama3.1:8b"
Write-Host "  2. .\.venv\Scripts\python.exe main.py setup-voice"
Write-Host "  3. .\.venv\Scripts\python.exe main.py doctor"
Write-Host "  4. start.bat      (dashboard at http://127.0.0.1:8787)"
