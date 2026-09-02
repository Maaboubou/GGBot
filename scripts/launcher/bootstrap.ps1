param(
    [switch]$Startup
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
$Requirements = Join-Path $ProjectRoot "requirements.txt"
$HashMarker = Join-Path $VenvDir ".requirements.sha256"
$InstallScript = Join-Path $ProjectRoot "scripts\install.ps1"
$AppIcon = Join-Path $ProjectRoot "mabobot_launcher\assets\mabobot.ico"
$AppIconImage = Join-Path $ProjectRoot "mabobot_launcher\assets\mabobot-icon.png"
$LogDir = Join-Path $ProjectRoot "logs"
$BootstrapLog = Join-Path $LogDir "launcher_bootstrap.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-BootstrapLog([string]$Message) {
    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $BootstrapLog -Value "$Timestamp $Message" -Encoding UTF8
}

function Close-BootstrapMutex {
    if ($script:OwnsBootstrapMutex) {
        try { $script:BootstrapMutex.ReleaseMutex() } catch { }
        $script:OwnsBootstrapMutex = $false
    }
    if ($script:BootstrapMutex) {
        $script:BootstrapMutex.Dispose()
        $script:BootstrapMutex = $null
    }
}

function Show-ErrorDialog([string]$Message) {
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            $Message,
            "Mabobot 启动失败",
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        ) | Out-Null
    }
    catch {
        Write-BootstrapLog "无法显示错误窗口：$($_.Exception.Message)"
    }
}

function Test-EnvironmentReady {
    if (-not (Test-Path $VenvPython) -or -not (Test-Path $VenvPythonw)) {
        return $false
    }
    if (-not (Test-Path $Requirements) -or -not (Test-Path $HashMarker)) {
        return $false
    }
    $CurrentHash = (Get-FileHash $Requirements -Algorithm SHA256).Hash
    $SavedHash = (Get-Content $HashMarker -Raw).Trim()
    if ($CurrentHash -ne $SavedHash) {
        return $false
    }
    & $VenvPython -c "import webview, pystray, psutil, dotenv, win32api, win32con, win32event, win32gui, win32process, win32security, win32ts, win32ui" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function New-ProgressWindow {
    Add-Type -AssemblyName PresentationFramework
    Add-Type -AssemblyName System.Windows.Forms
    [xml]$Xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        Width="430" Height="214" WindowStyle="None" AllowsTransparency="True"
        Background="Transparent" ResizeMode="NoResize" WindowStartupLocation="CenterScreen"
        ShowInTaskbar="True" Topmost="True">
    <Border CornerRadius="16" BorderBrush="#E6DFD8" BorderThickness="1" Background="#FFFEFA" Padding="26">
        <Border.Effect><DropShadowEffect BlurRadius="28" ShadowDepth="8" Opacity="0.18" Color="#3A2D25"/></Border.Effect>
        <Grid>
            <Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
            <StackPanel Orientation="Horizontal">
                <Image Width="38" Height="38" Source="$AppIconImage" Stretch="Uniform"/>
                <StackPanel Margin="13,0,0,0">
                    <TextBlock Text="正在准备 Mabobot" Foreground="#141413" FontFamily="Microsoft YaHei UI" FontSize="16" FontWeight="SemiBold"/>
                    <TextBlock Text="首次运行会安装依赖与浏览器组件" Foreground="#6C6A64" FontFamily="Microsoft YaHei UI" FontSize="11" Margin="0,5,0,0"/>
                </StackPanel>
            </StackPanel>
            <ProgressBar Grid.Row="1" Height="5" Margin="0,25,0,0" IsIndeterminate="True" Foreground="#CC785C" Background="#F5F0E8" BorderThickness="0"/>
            <TextBlock Name="StatusText" Grid.Row="2" Text="正在检查运行环境…" Foreground="#5F5B55" FontFamily="Microsoft YaHei UI" FontSize="11" Margin="0,14,0,0" TextTrimming="CharacterEllipsis" ToolTip="{Binding Text, RelativeSource={RelativeSource Self}}"/>
            <TextBlock Grid.Row="3" Text="请稍候，不要关闭电脑" Foreground="#96928B" FontFamily="Microsoft YaHei UI" FontSize="10" Margin="0,7,0,0"/>
        </Grid>
    </Border>
</Window>
"@
    $Reader = New-Object System.Xml.XmlNodeReader $Xaml
    $Window = [Windows.Markup.XamlReader]::Load($Reader)
    if (Test-Path -LiteralPath $AppIcon) {
        $Window.Icon = [System.Windows.Media.Imaging.BitmapFrame]::Create([System.Uri]$AppIcon)
    }
    return $Window
}

function Invoke-Installer {
    if (-not (Test-Path $InstallScript)) {
        throw "找不到环境安装脚本：$InstallScript"
    }

    $Window = New-ProgressWindow
    $StatusText = $Window.FindName("StatusText")
    $StatusFile = Join-Path $LogDir "launcher_bootstrap.status"
    Set-Content -LiteralPath $StatusFile -Value "正在启动环境检查…" -Encoding UTF8
    $Window.Show()
    try {
        $Info = New-Object System.Diagnostics.ProcessStartInfo
        $Info.FileName = "powershell.exe"
        $Info.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$InstallScript`" -StatusFile `"$StatusFile`""
        $Info.WorkingDirectory = $ProjectRoot
        $Info.UseShellExecute = $false
        $Info.CreateNoWindow = $true
        $Info.RedirectStandardOutput = $true
        $Info.RedirectStandardError = $true

        $Process = New-Object System.Diagnostics.Process
        $Process.StartInfo = $Info
        if (-not $Process.Start()) {
            throw "无法启动环境安装进程"
        }
        $OutputTask = $Process.StandardOutput.ReadToEndAsync()
        $ErrorTask = $Process.StandardError.ReadToEndAsync()
        $LastStatus = ""
        while (-not $Process.HasExited) {
            if (Test-Path $StatusFile) {
                $RawStatus = Get-Content -LiteralPath $StatusFile -Raw -ErrorAction SilentlyContinue
                $CurrentStatus = if ($null -eq $RawStatus) { "" } else { ([string]$RawStatus).Trim() }
                if ($CurrentStatus -and $CurrentStatus -ne $LastStatus) {
                    $StatusText.Text = $CurrentStatus
                    $LastStatus = $CurrentStatus
                }
            }
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 100
        }
        $Process.WaitForExit()
        $Output = $OutputTask.GetAwaiter().GetResult()
        $ErrorOutput = $ErrorTask.GetAwaiter().GetResult()
        if ($Output) { Add-Content -LiteralPath $BootstrapLog -Value $Output -Encoding UTF8 }
        if ($ErrorOutput) { Add-Content -LiteralPath $BootstrapLog -Value $ErrorOutput -Encoding UTF8 }
        if ($Process.ExitCode -ne 0) {
            throw "环境安装失败，退出代码 $($Process.ExitCode)"
        }
    }
    finally {
        $Window.Close()
        Remove-Item -LiteralPath $StatusFile -Force -ErrorAction SilentlyContinue
    }
}

$BootstrapMutex = New-Object System.Threading.Mutex($false, "Local\MabobotBootstrapV3")
$OwnsBootstrapMutex = $false
try {
    $OwnsBootstrapMutex = $BootstrapMutex.WaitOne(0, $false)
}
catch [System.Threading.AbandonedMutexException] {
    $OwnsBootstrapMutex = $true
}
if (-not $OwnsBootstrapMutex) {
    Write-BootstrapLog "已有启动引导正在运行，本次请求退出"
    Close-BootstrapMutex
    exit 0
}

try {
    Set-Location $ProjectRoot
    Write-BootstrapLog "启动引导开始，Startup=$Startup"
    if (-not (Test-EnvironmentReady)) {
        Write-BootstrapLog "环境未就绪，开始执行安装脚本"
        Invoke-Installer
    }
    if (-not (Test-EnvironmentReady)) {
        throw "安装结束后环境检查仍未通过"
    }

    $Arguments = @("-m", "mabobot_launcher")
    if ($Startup) {
        $Arguments += "--startup"
    }
    Start-Process -FilePath $VenvPythonw -ArgumentList $Arguments -WorkingDirectory $ProjectRoot
    Write-BootstrapLog "桌面启动器进程已创建"
    Close-BootstrapMutex
    exit 0
}
catch {
    $Message = $_.Exception.Message
    Write-BootstrapLog "启动失败：$Message"
    Show-ErrorDialog "Mabobot 无法启动。`n`n$Message`n`n详细日志：$BootstrapLog"
    Close-BootstrapMutex
    exit 1
}
