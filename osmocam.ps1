# Starts OsmoCam. The first run creates a private Python environment and installs the requirements
# (needs internet once). Arguments pass through to osmocam.py, e.g.  .\osmocam.ps1 --no-vcam
# The environment lives at a short path on purpose: the Bluetooth (WinRT) DLLs fail to load from long paths.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $env:LOCALAPPDATA 'osmo-pc\venv-cam'
$python = Join-Path $venv 'Scripts\python.exe'
$requirements = Join-Path $here 'requirements.txt'
$stamp = Join-Path $venv 'requirements.sha256'

# An install that never finished: start over (and choose the Python again).
if ((Test-Path $venv) -and -not (Test-Path $stamp)) { Remove-Item -Recurse -Force $venv }
if (-not (Test-Path $python)) {
    Write-Host 'First run: creating the Python environment...'
    # Newest first, but Python 3.15 (October 2026) still lacks builds of some of the libraries: it is
    # only used when nothing from 3.10 to 3.14 is installed.
    # Only versions already installed, as "py -0" lists them: the Python install manager would download
    # a missing one by itself.
    $ErrorActionPreference = 'Continue'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $installed = py -0 2>$null | Out-String
        foreach ($v in '3.14', '3.13', '3.12', '3.11', '3.10', '3') {
            if ($installed -match "-(V:)?$([regex]::Escape($v))\b") { py "-$v" -m venv $venv 2>$null }
            if (Test-Path $python) { break }
        }
    }
    if (-not (Test-Path $python) -and (Get-Command python -ErrorAction SilentlyContinue)) { python -m venv $venv }
    $ErrorActionPreference = 'Stop'
    if (-not (Test-Path $python)) {
        Write-Host 'Python 3.10 or newer is required (3.14 recommended): https://www.python.org/downloads/windows/'
        exit 1
    }
}
$hash = (Get-FileHash $requirements -Algorithm SHA256).Hash
if (-not (Test-Path $stamp) -or (Get-Content $stamp) -ne $hash) {
    Write-Host 'Installing the requirements...'
    & $python -m pip install --disable-pip-version-check -q -r $requirements
    if ($LASTEXITCODE -ne 0) {
        $version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
        Remove-Item -Recurse -Force $venv
        Write-Host "Installing the requirements failed (Python $version). Check the internet connection."
        if ([version]$version -gt [version]'3.14') {
            Write-Host "Python $version is too new for some of the libraries: install Python 3.14 from"
            Write-Host 'https://www.python.org/downloads/windows/ and run this again.'
        }
        exit 1
    }
    Set-Content -Path $stamp -Value $hash -Encoding ascii
}
& $python (Join-Path $here 'osmocam.py') @args
