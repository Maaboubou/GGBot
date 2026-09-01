[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8888,
    [switch]$SkipHealthCheck
)

$ErrorActionPreference = 'Stop'

$tailscaleCommand = Get-Command tailscale.exe -ErrorAction SilentlyContinue
$tailscalePath = if ($tailscaleCommand) { $tailscaleCommand.Source } else { $null }
if (-not $tailscalePath) {
    $candidate = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $candidate) {
        $tailscalePath = $candidate
    }
}
if (-not $tailscalePath) {
    throw '未找到 tailscale.exe。请先在此机器安装并登录 Tailscale。'
}

$localUrl = "http://127.0.0.1:$Port"
if (-not $SkipHealthCheck) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$localUrl/health" -TimeoutSec 5
        if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 500) {
            throw "HTTP $($response.StatusCode)"
        }
    }
    catch {
        throw "本机服务尚未就绪：$localUrl/health。请先启动 mabowx，或明确使用 -SkipHealthCheck。详情：$($_.Exception.Message)"
    }
}

Write-Host "正在将当前 Tailscale 节点的 HTTPS Serve 转发到 $localUrl ..."
& $tailscalePath serve --bg --yes $localUrl
if ($LASTEXITCODE -ne 0) {
    throw "Tailscale Serve 配置失败（退出码 $LASTEXITCODE）。首次使用时，请先打开命令显示的 Tailscale 启用链接完成确认，再重新运行本脚本。"
}

Write-Host "正在添加仅 Tailnet 可见的 HTTP 兼容入口 :$Port ..."
& $tailscalePath serve --bg --yes --http=$Port $localUrl
if ($LASTEXITCODE -ne 0) {
    throw "Tailscale HTTP Serve 配置失败（退出码 $LASTEXITCODE）。HTTPS 入口可能已经生效；请运行 'tailscale serve status' 检查当前状态。"
}

Write-Host ''
Write-Host '当前 Serve 状态：'
& $tailscalePath serve status
if ($LASTEXITCODE -ne 0) {
    throw "无法读取 Tailscale Serve 状态（退出码 $LASTEXITCODE）。"
}

Write-Host ''
Write-Host "配置完成。远程设备既可使用 https://<机器名>.<tailnet>.ts.net，也可使用 http://<机器名>:$Port。"
Write-Host '本脚本不会启用 Tailscale Funnel，也不会修改 Tailnet ACL、黑名单或白名单。'
