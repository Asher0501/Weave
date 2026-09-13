# 跑 agora brainstorm（真实 LLM）——14_forum 目录下的 weave.yaml 里取 DeepSeek key，仅注入进程环境变量，绝不打印。
# 用法（在仓库根 13_weave 下执行）：
#   .\demos\forum\run_brainstorm_real.ps1
#   .\demos\forum\run_brainstorm_real.ps1 -Topic "你的主题" -Rounds 0   # 0 = 用场景配置(默认12)
#   .\demos\forum\run_brainstorm_real.ps1 -ForumDir D:\path\to\14_forum
param(
    [string]$Topic = "为 weave v0.4 组件库设计一段 5 分钟开发者上手教程的开篇方案",
    [string]$ForumDir = "",
    [string]$Db = ""
)
$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # 13_weave
if (-not $ForumDir) { $ForumDir = Join-Path (Split-Path -Parent $Repo) "14_forum" }
if (-not $Db) { $Db = Join-Path $Repo "data\forum_demo_latest.db" }

$weaveYaml = Join-Path $ForumDir "weave.yaml"
if (-not (Test-Path $weaveYaml)) { throw "找不到 $weaveYaml —— 用 -ForumDir 指定 14_forum" }

# 1) 读取 DeepSeek key（不回显）
$m = Select-String -Path $weaveYaml -Pattern '^\s*api_key:\s*(\S+)'
if (-not $m) { throw "$weaveYaml 中没有 api_key 字段" }
$env:DEEPSEEK_API_KEY = $m.Matches[0].Groups[1].Value
# agora 已全量运行于 weave v0.4，运行时不再需要 archive/v0.3
$env:PYTHONPATH = $Repo

$log = Join-Path $Repo "data\forum_demo_latest.log"
Remove-Item $Db, $log -ErrorAction SilentlyContinue

Write-Host "== agora × weave v0.4 真实 LLM brainstorm =="
Write-Host "forum : $ForumDir"
Write-Host "topic : $Topic"
Write-Host "db    : $Db"
Write-Host "key   : (已注入环境变量，长度 $($env:DEEPSEEK_API_KEY.Length))"
Write-Host "启动中（默认最多 12 轮，逐轮真实请求 DeepSeek）…"

Push-Location $ForumDir
try {
    python (Join-Path $Repo "demos\forum\run_agora_demo.py") run `
        --config "scenarios\brainstorm.yaml" `
        --topic $Topic `
        --db $Db *> $log
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

Write-Host "exit=$code"
if ($code -ne 0) {
    Write-Host "—— 失败，日志尾部："
    Get-Content $log -Tail 25
    exit $code
}

Write-Host "—— 成功。状态行与日志尾部："
Get-Content $log -Tail 30
Write-Host "完整运行记录：$log"
