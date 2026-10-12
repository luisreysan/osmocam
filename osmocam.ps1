# Starts OsmoCam. The first run creates a private Python environment and installs the requirements
# (needs internet once). Arguments pass through to osmocam.py, e.g.  .\osmocam.ps1 --no-vcam
# The environment lives at a short path on purpose: the Bluetooth (WinRT) DLLs fail to load from long paths.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $env:LOCALAPPDATA 'osmo-pc\venv-cam'
$python = Join-Path $venv 'Scripts\python.exe'
$requirements = Join-Path $here 'requirements.txt'
$stamp = Join-Path $venv 'requirements.sha256'

if (-not (Test-Path $python)) {
    Write-Host 'First run: creating the Python environment...'
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv $venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { python -m venv $venv }
    if (-not (Test-Path $python)) {
        Write-Host 'Python 3.10 or newer is required: https://www.python.org/downloads/windows/'
        exit 1
    }
}
$hash = (Get-FileHash $requirements -Algorithm SHA256).Hash
if (-not (Test-Path $stamp) -or (Get-Content $stamp) -ne $hash) {
    Write-Host 'Installing the requirements...'
    & $python -m pip install --disable-pip-version-check -q -r $requirements
    if ($LASTEXITCODE -ne 0) { Write-Host 'Installing the requirements failed.'; exit 1 }
    Set-Content -Path $stamp -Value $hash -Encoding ascii
}
& $python (Join-Path $here 'osmocam.py') @args
