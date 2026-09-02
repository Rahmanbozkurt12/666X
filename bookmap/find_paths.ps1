# Bookmap köprü — Windows yol bulucu
# Bu proje .jar üretmez. Configure add-ons ekranında jar aramayın.

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $RepoRoot "bookmap_telegram_bridge.py"))) {
    $RepoRoot = Get-Location
}

Write-Host ""
Write-Host "=== Bookmap köprü yolları ===" -ForegroundColor Cyan
Write-Host "Repo kökü: $RepoRoot"
Write-Host ""

$AddonPy = Join-Path $RepoRoot "bookmap\wall_alert_addon.py"
$BridgePy = Join-Path $RepoRoot "bookmap_telegram_bridge.py"
$EventsJsonl = Join-Path $RepoRoot "output\bookmap_events.jsonl"
$ConfigJson = Join-Path $RepoRoot "config\bookmap_alerts.json"

function Show-File($Label, $Path) {
    if (Test-Path -LiteralPath $Path) {
        $Full = (Resolve-Path -LiteralPath $Path).Path
        Write-Host "[OK]  $Label" -ForegroundColor Green
        Write-Host "      $Full"
    } else {
        Write-Host "[--]  $Label (henüz yok)" -ForegroundColor Yellow
        Write-Host "      $Path"
    }
}

Show-File "Add-on (.py) — Bookmap Scripts editöründe AÇILACAK dosya" $AddonPy
Show-File "Köprü (.py) — VS Code / PowerShell'de çalıştır" $BridgePy
Show-File "Config" $ConfigJson
Show-File "Canlı veri dosyası (add-on Enable sonrası oluşur)" $EventsJsonl

Write-Host ""
Write-Host "--- Bookmap uygulaması (add-on DEĞİL) ---" -ForegroundColor Cyan

$Candidates = @(
    "${env:ProgramFiles}\Bookmap\Bookmap.jar",
    "${env:ProgramFiles(x86)}\Bookmap\Bookmap.jar",
    "$env:LOCALAPPDATA\Bookmap\Bookmap.jar"
)
$FoundApp = $false
foreach ($p in $Candidates) {
    if (Test-Path -LiteralPath $p) {
        Write-Host "[OK]  Bookmap.jar (uygulama — Configure add-ons'a EKLEMEYİN)" -ForegroundColor Green
        Write-Host "      $p"
        $FoundApp = $true
    }
}
if (-not $FoundApp) {
    Write-Host "[--]  Bookmap.jar varsayılan yollarda bulunamadı (sorun değil)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "YANLIŞ: Settings > Configure add-ons > Add... ile .jar / .lnk seçmek" -ForegroundColor Red
Write-Host "DOĞRU:  Settings > Manage plugins > Bookmap Add-ons (L1) > Python API" -ForegroundColor Green
Write-Host "Sonra:  Python API / Scripts editöründe wall_alert_addon.py açıp Enable" -ForegroundColor Green
Write-Host ""
Write-Host "Detay: BOOKMAP.md" -ForegroundColor Cyan
Write-Host ""
