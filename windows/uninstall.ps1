$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName 'TankiRecruit' -Confirm:$false
Write-Host 'Задача TankiRecruit удалена; профиль и история приглашений сохранены.'
