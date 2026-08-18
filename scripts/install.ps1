param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $ProjectRoot "requirements.txt"
$HashMarker = Join-Path $VenvDir ".requirements.sha256"

function Write-Step([string]$Message) {
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Stop-Install([string]$Message) {
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    exit 1
}

Set-Location $ProjectRoot

if (-not (Test-Path $Requirements)) {
    Stop-Install "requirements.txt 不存在。"
}

$RequirementHash = (Get-FileHash $Requirements -Algorithm SHA256).Hash
$EnvironmentReady = $false
if (-not $Force -and (Test-Path $VenvPython) -and (Test-Path $HashMarker)) {
    $SavedHash = (Get-Content $HashMarker -Raw).Trim()
    if ($SavedHash -eq $RequirementHash) {
        & $VenvPython -c "import fastapi, flask, wxautox4" 2>$null
        $EnvironmentReady = ($LASTEXITCODE -eq 0)
    }
}

if (-not $EnvironmentReady) {
    $Candidates = @(
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "python"; Args = @() }
    )
    $PythonExe = $null
    $PythonArgs = @()

    foreach ($Candidate in $Candidates) {
        if (-not (Get-Command $Candidate.Exe -ErrorAction SilentlyContinue)) {
            continue
        }
        $Exe = $Candidate.Exe
        $ArgsPrefix = @($Candidate.Args)
        $Version = & $Exe @ArgsPrefix -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $Version -in @("3.11", "3.12")) {
            $PythonExe = $Exe
            $PythonArgs = $ArgsPrefix
            break
        }
    }

    if (-not $PythonExe) {
        Stop-Install "未找到 Python 3.11 或 3.12（64 位）。请先从 python.org 安装，并勾选 Add Python to PATH。"
    }

    if (-not (Test-Path $VenvPython)) {
        Write-Step "创建虚拟环境 .venv"
        & $PythonExe @PythonArgs -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            Stop-Install "创建虚拟环境失败。"
        }
    }

    Write-Step "更新 pip、setuptools 和 wheel"
    & $VenvPython -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "pip 基础工具更新失败，请检查网络或代理。"
    }

    Write-Step "安装公开版依赖（首次运行可能需要数分钟）"
    & $VenvPython -m pip install -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "依赖安装失败，请检查网络、Python 版本和上方 pip 错误。"
    }

    & $VenvPython -c "import fastapi, flask, wxautox4"
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "依赖安装完成，但关键模块导入检查失败。"
    }

    Set-Content -Path $HashMarker -Value $RequirementHash -Encoding ASCII
}
else {
    Write-Step "虚拟环境和依赖已就绪，跳过重复安装"
}

$EnvFile = Join-Path $ProjectRoot ".env"
$EnvExample = Join-Path $ProjectRoot ".env.example"
if (-not (Test-Path $EnvFile)) {
    Copy-Item $EnvExample $EnvFile
    Write-Step "已从 .env.example 创建本地 .env"
}

foreach ($Directory in @("data", "logs", "tmp")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot $Directory) | Out-Null
}

Write-Host "[OK] Python 环境准备完成：$VenvPython" -ForegroundColor Green
exit 0
