param([switch]$DebugMode, [switch]$SmokeTest, [switch]$ControlTest, [switch]$CraftEvidence, [switch]$LiveTest, [string]$LiveRoute='INSPECT', [string]$LiveResumeRecord='')
$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
Write-Host "Application workspace: $appRoot"
if ($LiveTest) { $env:LEIMING_TEST_ROUTE=$LiveRoute }
$env:LEIMING_RESUME_RECORD=$LiveResumeRecord
$pythonPath = Join-Path $appRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python environment missing in workspace: $appRoot`nExpected: $pythonPath`nRun once: `"$(Join-Path $appRoot 'Setup.cmd')`" then open Start.cmd in this same workspace."
}
# Unicode resource I/O is handled by the application; no drive mapping is needed.
$entry = if ($ControlTest) { 'tools\smoke_task_controls_windows.py' } elseif ($CraftEvidence) { 'tools\crafting_evidence.py' } elseif ($LiveTest) { 'tools\live_test.py' } elseif ($SmokeTest) { 'tools\smoke_gui.py' } elseif ($DebugMode) { 'main_debug.py' } else { 'main.py' }
Push-Location $appRoot
try {
    & $pythonPath -X utf8 (Join-Path $appRoot $entry)
    $appExitCode = $LASTEXITCODE
} finally { Pop-Location }
if ($appExitCode -ne 0) { throw "Application exited with code $appExitCode; see logs." }
