# Bookmap Add + Python runtime - Windows helper

$src = "C:\Bookmap\Python\build\yenibot.jar"
$dstDir = "$env:USERPROFILE\OneDrive\Desktop\bot"
if (-not (Test-Path $dstDir)) { $dstDir = "$env:USERPROFILE\Desktop\bot" }
New-Item -ItemType Directory -Force -Path $dstDir | Out-Null

if (Test-Path $src) {
    Copy-Item $src "$dstDir\yenibot.jar" -Force
    Write-Host "JAR KOPYALANDI:" -ForegroundColor Green
    Write-Host "  $dstDir\yenibot.jar"
} else {
    Write-Host "Bulunamadi: $src" -ForegroundColor Yellow
    Write-Host "Build folder jar listesi:"
    Get-ChildItem "C:\Bookmap\Python\build\*.jar" -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $($_.FullName)" }
}

Write-Host ""
Write-Host "Python yollari (Set custom runtime icin .exe secin):" -ForegroundColor Cyan
$paths = @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
)
foreach ($p in $paths) {
    if (Test-Path $p) {
        $v = & $p -c "import sys; print(sys.version)"
        Write-Host "  $p"
        Write-Host "    $v"
    }
}

Write-Host ""
Write-Host "ADD: Configure add-ons -> Add... -> adres cubuguna yapistir:"
Write-Host "  $dstDir"
Write-Host "Sonra yenibot.jar sec."
Write-Host ""
Write-Host "RUNTIME HATASI: Python 3.13 ise 3.12 kurun, Set custom runtime ile python.exe secin."
