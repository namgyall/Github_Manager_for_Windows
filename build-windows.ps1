$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutputDirectory = Join-Path $ProjectRoot "dist"
$Executable = Join-Path $OutputDirectory "Github Manager.exe"

$Python = (Get-Command python -ErrorAction Stop).Source

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  Building Github Manager for Windows x64           " -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan

Write-Host "1. Installing app dependencies..." -ForegroundColor Yellow
& $Python -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Installing app dependencies failed." }

Write-Host "2. Checking PyInstaller..." -ForegroundColor Yellow
& $Python -m pip install "pyinstaller>=6.14"
if ($LASTEXITCODE -ne 0) { throw "Installing PyInstaller failed." }

$BuildDirectory = Join-Path ([System.IO.Path]::GetTempPath()) (
    "GithubManager-PyInstaller-" + [guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $BuildDirectory -Force | Out-Null

try {
    Write-Host "3. Creating Windows application icon..." -ForegroundColor Yellow
    $IconPath = Join-Path $BuildDirectory "GithubManager.ico"
    & $Python (Join-Path $ProjectRoot "make_icon.py") $IconPath
    if ($LASTEXITCODE -ne 0) { throw "Creating the Windows app icon failed." }

    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    Remove-Item -LiteralPath $Executable -Force -ErrorAction SilentlyContinue

    Write-Host "4. Packaging standalone executable with PyInstaller..." -ForegroundColor Yellow
    & $Python -m PyInstaller `
        --noconfirm `
        --onefile `
        --windowed `
        --name "Github Manager" `
        --icon $IconPath `
        --add-data "$(Join-Path $ProjectRoot 'Resources');Resources" `
        --distpath $OutputDirectory `
        --workpath $BuildDirectory `
        --specpath $BuildDirectory `
        (Join-Path $ProjectRoot "main.py")

    if ($LASTEXITCODE -ne 0) { throw "PyInstaller compilation failed." }
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw "Standalone executable was not generated: $Executable"
    }

    Write-Host ""
    Write-Host "====================================================" -ForegroundColor Green
    Write-Host "  SUCCESS: Standalone executable created:          " -ForegroundColor Green
    Write-Host "  $Executable" -ForegroundColor Green
    Write-Host "====================================================" -ForegroundColor Green
}
finally {
    if (Test-Path -LiteralPath $BuildDirectory) {
        Remove-Item -LiteralPath $BuildDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}
