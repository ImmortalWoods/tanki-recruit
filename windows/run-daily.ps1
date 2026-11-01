$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
$data = if ($env:TANKI_RECRUIT_DATA) { $env:TANKI_RECRUIT_DATA } else { Join-Path $env:LOCALAPPDATA 'tanki-recruit' }
New-Item -ItemType Directory -Force -Path $data | Out-Null
$errorLog = Join-Path $data 'recruit-errors.log'
$tempLog = [System.IO.Path]::GetTempFileName()
$env:PYTHONUTF8 = '1'
$code = 1
try {
    $ErrorActionPreference = 'Continue'
    & $python -u (Join-Path $project 'candidates.py') --send 2>&1 | Out-File -LiteralPath $tempLog -Encoding UTF8
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 1 }
} catch {
    $_ | Out-File -LiteralPath $tempLog -Append -Encoding UTF8
    $code = 1
} finally {
    if ($code -ne 0) {
        Add-Content -LiteralPath $errorLog -Encoding UTF8 -Value "`n--- $(Get-Date -Format o): ошибка, код $code ---"
        Get-Content -LiteralPath $tempLog -Encoding UTF8 | Add-Content -LiteralPath $errorLog -Encoding UTF8
    }
    Remove-Item -LiteralPath $tempLog -Force -ErrorAction SilentlyContinue
}
exit $code
