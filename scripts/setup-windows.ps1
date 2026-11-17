$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (!(Test-Path 'bin\windows-x86_64\ai-rdp-native.exe')) {
    throw 'Use the Windows x64 release ZIP, which includes the native backend and DLLs.'
}
if (!(Test-Path '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+ x64 and enable its python command first.' }
}
& .\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Dependency wheels could not be installed. Use a supported x64 Python (3.14 is validated); no compiler is required.' }
Write-Host 'Ready. Run: .\.venv\Scripts\python.exe -m ai_rdp --help'
