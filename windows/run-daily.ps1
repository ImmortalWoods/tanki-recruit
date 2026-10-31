$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
$data = if ($env:TANKI_RECRUIT_DATA) { $env:TANKI_RECRUIT_DATA } else { Join-Path $env:LOCALAPPDATA 'tanki-recruit' }
New-Item -ItemType Directory -Force -Path $data | Out-Null
$log = Join-Path $data 'recruit.log'
$env:PYTHONUTF8 = '1'
Add-Content -LiteralPath $log -Encoding UTF8 -Value "`n--- $(Get-Date -Format o) ---"
try {
    # Записываем stderr вместе с stdout; код процесса сохраняется отдельно.
    $ErrorActionPreference = 'Continue'
    & $python -u (Join-Path $project 'candidates.py') --send 2>&1 | Out-File -LiteralPath $log -Append -Encoding UTF8
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 1 }
} catch {
    $_ | Out-File -LiteralPath $log -Append -Encoding UTF8
    $code = 1
}
if ($code -ne 0) {
    Add-Content -LiteralPath $log -Encoding UTF8 -Value "Ошибка, код $code"
}
exit $code
