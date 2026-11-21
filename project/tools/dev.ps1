<#
.SYNOPSIS
  一键启动 StoreDoctor 后端（并在终端打印前端启动命令）。

.EXAMPLE
  .\tools\dev.ps1
  .\tools\dev.ps1 -Port 8010 -Reload:$false
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$Python = "",
    [switch]$Reload,
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Resolve-Python {
    param([string]$Explicit)
    if ($Explicit) { return $Explicit }
    $venv = "E:\llm\third\test\k12\.venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    return "python"
}

$py = Resolve-Python -Explicit $Python

if (-not $SkipChecks) {
    if (-not (Test-Path ".\.env")) {
        Write-Host "[warn] 没有 .env，将按默认配置启动（无 key => 规则版 + mock 回放，功能不缺）" -ForegroundColor Yellow
    }
    if (-not (Test-Path ".\data\store.db")) {
        Write-Host "[warn] 没有 data\store.db，先跑：$py -m app.data.seed" -ForegroundColor Yellow
    }
    if (-not (Test-Path ".\web\node_modules")) {
        Write-Host "[info] 前端依赖未安装：cd web; npm install" -ForegroundColor Cyan
    }
}

$backend = "http://127.0.0.1:$Port"
Write-Host ""
Write-Host "后端  : $backend/api/health" -ForegroundColor Green
Write-Host "自检  : curl.exe -s $backend/api/health" -ForegroundColor DarkGray
Write-Host "前端  : 另开一个终端 ->" -ForegroundColor Green
Write-Host "        cd $root\web; npm run dev   (http://localhost:5173)" -ForegroundColor DarkGray
Write-Host ""

$argsList = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port")
if ($Reload) { $argsList += "--reload" }

& $py @argsList
