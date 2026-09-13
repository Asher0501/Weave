# 跑 agora 自身测试套件（回归：确认 weave v0.4 补丁没破坏 agora 的 FakeLLM/Repository/relay）。
# 用法（任意目录）：
#   .\demos\forum\run_agora_tests.ps1
param([string]$ForumDir = "")
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $ForumDir) { $ForumDir = Join-Path (Split-Path -Parent $Repo) "14_forum" }
if (-not (Test-Path (Join-Path $ForumDir "pyproject.toml"))) { throw "找不到 14_forum：用 -ForumDir 指定" }

$env:PYTHONPATH = $Repo
Push-Location $ForumDir
try {
    python -m pytest -q -p no:cacheprovider
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
