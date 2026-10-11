#Requires -RunAsAdministrator
# Engent'i Windows arka plan görevi olarak kaydeder:
#  - açılışta başlar, oturum açmayı beklemez (S4U: parola saklanmaz), masaüstünde pencere açmaz
#  - yönetici YETKİSİZ çalışır (RunLevel Limited): sistem dizinleri Windows ACL'leriyle korunur
#  - 5 dakikada bir bekçi tetikleyicisi: süreç ölmüşse yeniden başlatır, çalışıyorsa dokunmaz
# Kaldırmak: .\install.ps1 -Uninstall
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"
$name = "Engent"
$root = $PSScriptRoot
Set-Location $root  # "-m engent.policy" resolves the package from the current directory

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Engent görevi kaldırıldı."
    return
}

foreach ($f in ".env", "projects.json") {
    if (-not (Test-Path "$root\$f")) { throw "$f yok. Örnek dosyadan oluşturup doldur (.env.example / projects.example.json)." }
}
$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { python -m venv "$root\.venv" }
& $py -m pip install -q --upgrade pip
& $py -m pip install -q -r "$root\requirements.txt"
& $py -m engent.policy
if ($LASTEXITCODE -ne 0) { throw "Guardrail öz-testi başarısız." }

$action = New-ScheduledTaskAction -Execute $py -Argument "-m engent" -WorkingDirectory $root
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "Engent kuruldu ve başlatıldı. Telefonuna '🟢 Engent çevrimiçi' mesajı gelmeli. Log: $root\data\engent.log"
