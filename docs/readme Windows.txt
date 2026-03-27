#  新机器要修改我哦，
$ProjectRoot = "D:\katu\flow_captcha_service-main"
$Python312InstallerUrl = "https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe"
$Python312InstallerPath = "$env:TEMP\python-3.12.0-amd64.exe"

Set-Location $ProjectRoot
$ErrorActionPreference = "Stop"

function Get-Python312Exe {
    $candidatePaths = @(
        "C:\Program Files\Python312\python.exe",
        "$env:LocalAppData\Programs\Python\Python312\python.exe"
    )

    foreach ($candidate in $candidatePaths) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -c "import sys; print(sys.executable)"
        if ($LASTEXITCODE -eq 0) {
            $resolved = (& py -3.12 -c "import sys; print(sys.executable)" | Select-Object -Last 1).Trim()
            if ($resolved -and (Test-Path $resolved)) {
                return $resolved
            }
        }
    }

    return $null
}

# 不要复用从别的机器拷过来的 .venv，Windows 服务器上重建一次
if (Test-Path "$ProjectRoot\.venv") {
    Remove-Item "$ProjectRoot\.venv" -Recurse -Force
}

$LegacyImportHits = Get-ChildItem -Path "$ProjectRoot" -Recurse -Include *.py | Select-String -Pattern "CommonFramePackage" -SimpleMatch
if ($LegacyImportHits) {
    Write-Host "检测到旧版本遗留导入 CommonFramePackage，当前目录不是干净的最新代码。" -ForegroundColor Red
    Write-Host "请删除服务器上的旧项目目录后，重新完整覆盖上传当前仓库代码。" -ForegroundColor Yellow
    Write-Host "命中的文件如下：" -ForegroundColor Yellow
    $LegacyImportHits | ForEach-Object { Write-Host $_.Path -ForegroundColor Yellow }
    return
}

$PythonExe = Get-Python312Exe
if (-not $PythonExe) {
    Write-Host "未检测到 Python 3.12，开始自动下载安装。" -ForegroundColor Yellow
    Write-Host "下载地址: $Python312InstallerUrl" -ForegroundColor Yellow

    Invoke-WebRequest -Uri $Python312InstallerUrl -OutFile $Python312InstallerPath
    Start-Process -FilePath $Python312InstallerPath -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1" -Wait

    $PythonExe = Get-Python312Exe
    if (-not $PythonExe) {
        Write-Host "Python 3.12 安装完成后仍未找到 python.exe。" -ForegroundColor Red
        Write-Host "请手动执行安装器，然后重新打开 PowerShell 再执行本文件。" -ForegroundColor Yellow
        Write-Host "安装器路径: $Python312InstallerPath" -ForegroundColor Yellow
        return
    }
}

& $PythonExe --version
& $PythonExe -m venv "$ProjectRoot\.venv"
if ($LASTEXITCODE -ne 0 -or !(Test-Path "$ProjectRoot\.venv\Scripts\python.exe")) {
    Write-Host "创建虚拟环境失败。" -ForegroundColor Red
    Write-Host "请先确认 Python 3.12 已正确安装，再重新执行本文件。" -ForegroundColor Yellow
    return
}

# 后续统一通过绝对路径直接调用虚拟环境里的 Python
& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install -r "$ProjectRoot\requirements.txt"
& "$ProjectRoot\.venv\Scripts\python.exe" -m playwright install chromium

if (!(Test-Path "$ProjectRoot\data")) {
    New-Item -ItemType Directory -Path "$ProjectRoot\data" | Out-Null
}

if (!(Test-Path "$ProjectRoot\data\setting.toml")) {
    Copy-Item "$ProjectRoot\config\setting_example.toml" "$ProjectRoot\data\setting.toml"
}

# 启动服务
& "$ProjectRoot\.venv\Scripts\python.exe" "$ProjectRoot\main.py"

# 如果你只是想先进入虚拟环境，再手动执行命令
# & "$ProjectRoot\.venv\Scripts\Activate.ps1"

# 启动后验证
# curl http://127.0.0.1:8088/api/v1/health

# 如果这台机器只跑 master，不需要本地浏览器
# & "$ProjectRoot\.venv\Scripts\python.exe" -m pip install -r "$ProjectRoot\requirements.master.txt"

# Python 3.12 下载地址
# https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe
