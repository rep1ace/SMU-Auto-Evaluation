[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "1.0.0"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "正在安装 uv..."
    powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

uv sync --group build
if ($LASTEXITCODE -ne 0) { throw "安装构建依赖失败。" }
uv run pyinstaller --noconfirm --clean --windowed --name SMUAutoEvaluation `
    --icon "assets\icon.ico" --collect-all pystray --hidden-import PIL._tkinter_finder `
    --add-data "models;models" --add-data "assets;assets" tray_app.py
if ($LASTEXITCODE -ne 0) { throw "应用打包失败。" }

$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $commonPaths = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $iscc = $commonPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "未找到 Inno Setup 6，请先从 https://jrsoftware.org/isdl.php 安装。" }
& $iscc "/DMyAppVersion=$Version" installer\SMUAutoEvaluation.iss
if ($LASTEXITCODE -ne 0) { throw "安装包编译失败。" }
Write-Host "安装包已生成到 dist-installer 目录。"
