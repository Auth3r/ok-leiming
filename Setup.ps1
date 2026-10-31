$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
Write-Host "Preparing this workspace only: $PSScriptRoot"
# Each checkout owns its environment. pip's user cache reuses downloads;
# never copy/link another worktree's environment or update its dependencies.
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $hasPython312 = $false
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        try {
            & py.exe -3.12 -c 'import sys; print(sys.version)' 2>$null
            $hasPython312 = $LASTEXITCODE -eq 0
        } catch {
            $hasPython312 = $false
        }
    }
    if ($hasPython312) { & py.exe -3.12 -m venv .venv }
    elseif (Test-Path -LiteralPath $bundledPython) { & $bundledPython -m venv .venv }
    else { throw "Install Python 3.12 x64, then run $(Join-Path $PSScriptRoot 'Setup.cmd') again." }
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python environment.' }
}
& '.\.venv\Scripts\python.exe' -m pip install --index-url https://pypi.org/simple/ --no-deps -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host "Ready: $(Join-Path $PSScriptRoot 'Start.cmd')"
Write-Host 'Environment belongs to this workspace. Begin with the read-only inspection task.'
