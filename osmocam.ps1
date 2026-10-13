# Starts OsmoCam. The first run creates a private Python environment and installs the requirements
# (needs internet once). Arguments pass through to osmocam.py, e.g.  .\osmocam.ps1 --no-vcam
# The environment lives at a short path on purpose: the Bluetooth (WinRT) DLLs fail to load from long paths.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $env:LOCALAPPDATA 'osmo-pc\venv-cam'
$python = Join-Path $venv 'Scripts\python.exe'
$requirements = Join-Path $here 'requirements.txt'
$stamp = Join-Path $venv 'requirements.sha256'
# Python versions with ready-made builds of every library used here, newest first. Python 3.15 (October
# 2026) still lacks some of them (pip then tries to compile them and fails), so it is not used.
$versions = '3.14', '3.13', '3.12', '3.11', '3.10'

# An install that never finished: start over (and choose the Python again).
if ((Test-Path $venv) -and -not (Test-Path $stamp)) { Remove-Item -Recurse -Force $venv }
if (-not (Test-Path $python)) {
    Write-Host 'First run: creating the Python environment...'
    $ErrorActionPreference = 'Continue'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        # Only versions already installed, as "py -0" lists them: the Python install manager would
        # download a missing one by itself.
        $installed = py -0 2>$null | Out-String
        foreach ($v in $versions) {
            if ($installed -match "-(V:)?$([regex]::Escape($v))\b") { py "-$v" -m venv $venv 2>$null }
            if (Test-Path $python) { break }
        }
    }
    if (-not (Test-Path $python) -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $v = python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($versions -contains $v) { python -m venv $venv }
    }
    $ErrorActionPreference = 'Stop'
    if (-not (Test-Path $python)) {
        Write-Host ''
        Write-Host 'Python 3.10 to 3.14 is needed (3.14 recommended). Python 3.15 is too new for now: some of the'
        Write-Host 'libraries this app uses have no build for it yet. Install Python 3.14 (it can stay next to other'
        Write-Host 'versions) from https://www.python.org/downloads/windows/ or, with the Python install manager,'
        Write-Host 'with the command:  py install 3.14   Then start this again.'
        exit 1
    }
}
$hash = (Get-FileHash $requirements -Algorithm SHA256).Hash
if (-not (Test-Path $stamp) -or (Get-Content $stamp) -ne $hash) {
    Write-Host 'Installing the requirements...'
    & $python -m pip install --disable-pip-version-check -q --only-binary=:all: -r $requirements
    if ($LASTEXITCODE -ne 0) {
        $version = & $python -c "import sys, struct; print('%d.%d, %d-bit' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8))"
        Remove-Item -Recurse -Force $venv
        Write-Host "Installing the requirements failed (Python $version). Check the internet connection and the"
        Write-Host 'message above, then start this again.'
        exit 1
    }
    Set-Content -Path $stamp -Value $hash -Encoding ascii
}
& $python (Join-Path $here 'osmocam.py') @args
exit $LASTEXITCODE
