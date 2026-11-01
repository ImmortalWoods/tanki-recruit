$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Сначала установите зависимости в .venv по инструкции README.' }
$data = if ($env:TANKI_RECRUIT_DATA) { $env:TANKI_RECRUIT_DATA } else { Join-Path $env:LOCALAPPDATA 'tanki-recruit' }
if (-not (Test-Path -LiteralPath (Join-Path $data 'profiles\main'))) { throw 'Сначала войдите: .venv\Scripts\python.exe login.py main' }
$script = Join-Path $PSScriptRoot 'run-daily.ps1'
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At '09:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'TankiRecruit' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Задача TankiRecruit установлена на 09:00. Журнал: $project\logs; ошибки запуска: $data\recruit-errors.log"
