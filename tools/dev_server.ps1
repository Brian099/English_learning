# Local dev server (Windows)
#
#   tools\dev_server.cmd        (double-click, or run from a terminal)
#   powershell -ExecutionPolicy Bypass -File tools\dev_server.ps1
#
# Layout mirrors the container mounts:
#   media\     <- media root (container /media, read-only): copy videos here
#   .data\     <- database + secret (container /data)
#   .cache\    <- extracted audio, ASR input (container /cache)
#   .hf-cache\ <- whisper model (container /models)
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as ANSI
# and non-ASCII characters break parsing.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:PYTHONPATH      = "$root\.server-libs;$root\.asr-libs"
$env:DATABASE_URL    = "sqlite+aiosqlite:///$($root.Replace('\','/'))/.data/kids.db"
$env:MEDIA_ROOT      = "$root\media"
$env:CACHE_ROOT      = "$root\.cache"
$env:DATA_DIR        = "$root\.data"
$env:MODEL_DIR       = "$root\.hf-cache"
$env:TMP             = "$root\.tmp"
$env:TEMP            = "$root\.tmp"
$env:TZ              = "Asia/Shanghai"

# ingest pipeline
$env:AUTO_INGEST     = "1"
$env:NORMALIZE_AUDIO = "1"
$env:NORMALIZE_VIDEO = "0"
$env:TARGET_LUFS     = "-16"
$env:WHISPER_MODEL   = "small.en"
$env:WHISPER_THREADS = "3"
$env:HF_ENDPOINT     = "https://hf-mirror.com"

# empty = auto-generate APP_SECRET on first boot; password defaults to kids1234
$env:APP_SECRET      = ""
$env:PARENT_PASSWORD = ""

foreach ($dir in @($env:MEDIA_ROOT, $env:DATA_DIR, $env:CACHE_ROOT, $env:MODEL_DIR, $env:TMP)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Write-Host "media root : $env:MEDIA_ROOT"
Write-Host "database   : $env:DATABASE_URL"
Write-Host ""
Write-Host "parent  http://127.0.0.1:8010/"
Write-Host "student http://127.0.0.1:8010/student.html"
Write-Host "diag    http://127.0.0.1:8010/diag.html"
Write-Host "password: kids1234"
Write-Host ""

Set-Location "$root\server"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
