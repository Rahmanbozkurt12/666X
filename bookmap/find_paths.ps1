# Bookmap köprü — Windows yol bulucu
# .py → Bookmap editöründe Build → .jar oluşur. Plugins manager jar seçmez.

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

Show-File "Kaynak .py (editöre yapıştırılacak)" $AddonPy
Show-File "Köprü .py (VS Code'da çalıştır)" $BridgePy
Show-File "Config" $ConfigJson
Show-File "Canlı veri JSONL (Enable sonrası)" $EventsJsonl

Write-Host ""
Write-Host "--- Build folder (jar burada oluşur) ---" -ForegroundColor Cyan
$BuildRoots = @(
    "C:\Bookmap\Python",
    "$env:USERPROFILE\Bookmap\Python",
    "$env:LOCALAPPDATA\Bookmap",
    "C:\Bookmap"
)
$jars = @()
foreach ($root in $BuildRoots) {
    if (Test-Path -LiteralPath $root) {
        $jars += Get-ChildItem -LiteralPath $root -Recurse -Filter "*.jar" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notmatch '^(Bookmap|bm-strategy)' } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 8
    }
}
if ($jars.Count -gt 0) {
    $jars | Select-Object -First 8 | ForEach-Object {
        Write-Host "[OK]  $($_.FullName)  ($($_.LastWriteTime))" -ForegroundColor Green
    }
} else {
    Write-Host "[--]  Henüz build jar yok. Editörde Build + File→Open build folder kullanın." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Sıra:" -ForegroundColor Cyan
Write-Host "  1) Plugins manager: Python API kurulu (Remove görünüyorsa OK)"
Write-Host "  2) Settings → Configure add-ons → Python API tik → Open embedded editor"
Write-Host "  3) wall_alert_addon.py yapıştır → Build → Open build folder"
Write-Host "  4) Configure add-ons → Add... → build folder'daki .jar (lnk değil) → mavi tik"
Write-Host "  5) python bookmap_telegram_bridge.py --dry-run"
Write-Host ""
