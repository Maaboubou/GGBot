Set-StrictMode -Version 2.0

$script:PythonFallbackVersion = "3.12.10"
$script:PythonFallbackUrl = (
    "https://www.python.org/ftp/python/{0}/python-{0}-amd64.exe" -f
    $script:PythonFallbackVersion
)
$script:WebView2BootstrapUrl = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
$script:WebView2ClientId = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

function Send-MabobotRuntimeStatus {
    param(
        [scriptblock]$Status,
        [string]$Message
    )
    if ($null -ne $Status) {
        & $Status $Message
    }
}

function Get-MabobotPythonInfo {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$Arguments = @()
    )

    try {
        $ProbeArguments = @($Arguments) + @(
            "-c",
            "import struct,sys; print('%d.%d|%d|%s' % (sys.version_info[0],sys.version_info[1],struct.calcsize('P')*8,sys.executable))"
        )
        $ProbeOutput = @(& $FilePath @ProbeArguments 2>$null)
        $ProbeExitCode = $LASTEXITCODE
    }
    catch {
        return $null
    }

    if ($ProbeExitCode -ne 0 -or $ProbeOutput.Count -eq 0) {
        return $null
    }
    $ProbeLine = ([string]$ProbeOutput[-1]).Trim()
    if ($ProbeLine -notmatch '^(3\.(?:11|12))\|64\|(.+)$') {
        return $null
    }

    return [pscustomobject]@{
        FilePath = $FilePath
        Arguments = @($Arguments)
        Version = $Matches[1]
        Architecture = "64"
        Executable = $Matches[2]
    }
}

function Get-MabobotCompatiblePython {
    [CmdletBinding()]
    param()

    $Candidates = New-Object System.Collections.Generic.List[object]
    $Seen = @{}

    foreach ($LauncherSpec in @(
        @{ Command = "py"; Arguments = @("-3.12") },
        @{ Command = "py"; Arguments = @("-3.11") },
        @{ Command = "python"; Arguments = @() }
    )) {
        $Resolved = Get-Command $LauncherSpec.Command -CommandType Application -ErrorAction SilentlyContinue
        if ($null -eq $Resolved) {
            continue
        }
        # Windows' App Execution Alias stubs may open the Microsoft Store when
        # probed. They are not an installed interpreter, so skip them here.
        if ([string]$Resolved.Source -match '\\Microsoft\\WindowsApps\\') {
            continue
        }
        $Key = "$($Resolved.Source)|$($LauncherSpec.Arguments -join ' ')".ToLowerInvariant()
        if (-not $Seen.ContainsKey($Key)) {
            $Seen[$Key] = $true
            $Candidates.Add([pscustomobject]@{
                FilePath = [string]$Resolved.Source
                Arguments = @($LauncherSpec.Arguments)
            })
        }
    }

    foreach ($Version in @("3.12", "3.11")) {
        foreach ($RegistryPath in @(
            "Registry::HKEY_CURRENT_USER\Software\Python\PythonCore\$Version\InstallPath",
            "Registry::HKEY_LOCAL_MACHINE\Software\Python\PythonCore\$Version\InstallPath",
            "Registry::HKEY_LOCAL_MACHINE\Software\WOW6432Node\Python\PythonCore\$Version\InstallPath"
        )) {
            try {
                $RegistryKey = Get-Item -LiteralPath $RegistryPath -ErrorAction Stop
                $Executable = [string]$RegistryKey.GetValue("ExecutablePath", "")
                if (-not $Executable) {
                    $InstallRoot = [string]$RegistryKey.GetValue("", "")
                    if ($InstallRoot) {
                        $Executable = Join-Path $InstallRoot "python.exe"
                    }
                }
                if (-not $Executable -or -not (Test-Path -LiteralPath $Executable)) {
                    continue
                }
                $Key = $Executable.ToLowerInvariant()
                if (-not $Seen.ContainsKey($Key)) {
                    $Seen[$Key] = $true
                    $Candidates.Add([pscustomobject]@{
                        FilePath = $Executable
                        Arguments = @()
                    })
                }
            }
            catch { }
        }
    }

    $KnownPaths = @()
    if ($env:LOCALAPPDATA) {
        $KnownPaths += Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
        $KnownPaths += Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
    }
    if ($env:ProgramFiles) {
        $KnownPaths += Join-Path $env:ProgramFiles "Python312\python.exe"
        $KnownPaths += Join-Path $env:ProgramFiles "Python311\python.exe"
    }
    foreach ($Executable in $KnownPaths) {
        if (-not (Test-Path -LiteralPath $Executable)) {
            continue
        }
        $Key = $Executable.ToLowerInvariant()
        if (-not $Seen.ContainsKey($Key)) {
            $Seen[$Key] = $true
            $Candidates.Add([pscustomobject]@{
                FilePath = $Executable
                Arguments = @()
            })
        }
    }

    foreach ($Candidate in $Candidates) {
        $Info = Get-MabobotPythonInfo `
            -FilePath $Candidate.FilePath `
            -Arguments @($Candidate.Arguments)
        if ($null -ne $Info) {
            return $Info
        }
    }
    return $null
}

function Get-MabobotWebView2Version {
    [CmdletBinding()]
    param()

    $RegistryPaths = @(
        "Registry::HKEY_CURRENT_USER\Software\Microsoft\EdgeUpdate\Clients\$script:WebView2ClientId",
        "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$script:WebView2ClientId",
        "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\EdgeUpdate\Clients\$script:WebView2ClientId"
    )
    foreach ($RegistryPath in $RegistryPaths) {
        try {
            $Version = [string](Get-ItemProperty -LiteralPath $RegistryPath -Name "pv" -ErrorAction Stop).pv
            if ($Version -and $Version -ne "0.0.0.0") {
                return $Version
            }
        }
        catch { }
    }
    return ""
}

function Test-MabobotWebView2Runtime {
    [CmdletBinding()]
    param()
    return [bool](Get-MabobotWebView2Version)
}

function Save-MabobotRuntimeInstaller {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url,
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    if ([enum]::GetNames([System.Net.SecurityProtocolType]) -contains "Tls12") {
        [System.Net.ServicePointManager]::SecurityProtocol = (
            [System.Net.ServicePointManager]::SecurityProtocol -bor
            [System.Net.SecurityProtocolType]::Tls12
        )
    }
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
    if (-not (Test-Path -LiteralPath $Destination)) {
        throw "Runtime installer download did not create a file."
    }
    if ((Get-Item -LiteralPath $Destination).Length -lt 1024) {
        throw "Runtime installer download is incomplete."
    }
}

function Assert-MabobotInstallerSignature {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$PublisherFragment
    )

    $Signature = Get-AuthenticodeSignature -LiteralPath $Path
    $Subject = if ($null -ne $Signature.SignerCertificate) {
        [string]$Signature.SignerCertificate.Subject
    }
    else {
        ""
    }
    if ([string]$Signature.Status -ne "Valid" -or $Subject -notmatch [regex]::Escape($PublisherFragment)) {
        throw "Runtime installer signature validation failed for publisher '$PublisherFragment'."
    }
}

function Install-MabobotPython {
    [CmdletBinding()]
    param([scriptblock]$Status)

    $Existing = Get-MabobotCompatiblePython
    if ($null -ne $Existing) {
        return $Existing
    }

    $Winget = Get-Command "winget" -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $Winget) {
        Send-MabobotRuntimeStatus $Status "正在通过 WinGet 安装 Python 3.12（当前用户）"
        & $Winget.Source install `
            --id Python.Python.3.12 `
            --exact `
            --source winget `
            --scope user `
            --architecture x64 `
            --silent `
            --disable-interactivity `
            --accept-package-agreements `
            --accept-source-agreements |
            ForEach-Object { Write-Host $_ }
        $WingetExitCode = $LASTEXITCODE
        $Installed = Get-MabobotCompatiblePython
        if ($null -ne $Installed) {
            return $Installed
        }
        Send-MabobotRuntimeStatus $Status "WinGet 安装未完成，正在切换到 Python 官方安装器"
        Write-Host "[WARN] WinGet Python install exit code: $WingetExitCode" -ForegroundColor Yellow
    }

    $DownloadDirectory = Join-Path ([System.IO.Path]::GetTempPath()) "MabobotBootstrap"
    New-Item -ItemType Directory -Force -Path $DownloadDirectory | Out-Null
    $InstallerPath = Join-Path $DownloadDirectory "python-$script:PythonFallbackVersion-amd64.exe"
    try {
        Send-MabobotRuntimeStatus $Status "正在下载 Python $script:PythonFallbackVersion 官方安装器"
        Save-MabobotRuntimeInstaller -Url $script:PythonFallbackUrl -Destination $InstallerPath
        Send-MabobotRuntimeStatus $Status "正在验证 Python 官方数字签名"
        Assert-MabobotInstallerSignature `
            -Path $InstallerPath `
            -PublisherFragment "Python Software Foundation"
        Send-MabobotRuntimeStatus $Status "正在为当前用户安装 Python $script:PythonFallbackVersion"
        $Installer = Start-Process `
            -FilePath $InstallerPath `
            -ArgumentList @(
                "/quiet",
                "InstallAllUsers=0",
                "PrependPath=1",
                "Include_launcher=1",
                "InstallLauncherAllUsers=0",
                "Include_pip=1",
                "Include_test=0",
                "Include_doc=0",
                "Shortcuts=0"
            ) `
            -Wait `
            -PassThru
        if ($Installer.ExitCode -ne 0) {
            throw "Python installer exited with code $($Installer.ExitCode)."
        }
    }
    finally {
        Remove-Item -LiteralPath $InstallerPath -Force -ErrorAction SilentlyContinue
    }

    $Installed = Get-MabobotCompatiblePython
    if ($null -eq $Installed) {
        throw "Python installation completed, but a supported 64-bit interpreter was not found."
    }
    return $Installed
}

function Install-MabobotWebView2Runtime {
    [CmdletBinding()]
    param([scriptblock]$Status)

    $ExistingVersion = Get-MabobotWebView2Version
    if ($ExistingVersion) {
        return $ExistingVersion
    }

    $DownloadDirectory = Join-Path ([System.IO.Path]::GetTempPath()) "MabobotBootstrap"
    New-Item -ItemType Directory -Force -Path $DownloadDirectory | Out-Null
    $InstallerPath = Join-Path $DownloadDirectory "MicrosoftEdgeWebView2Setup.exe"
    try {
        Send-MabobotRuntimeStatus $Status "正在下载 Microsoft WebView2 Runtime"
        Save-MabobotRuntimeInstaller -Url $script:WebView2BootstrapUrl -Destination $InstallerPath
        Send-MabobotRuntimeStatus $Status "正在验证 Microsoft 官方数字签名"
        Assert-MabobotInstallerSignature `
            -Path $InstallerPath `
            -PublisherFragment "Microsoft Corporation"
        Send-MabobotRuntimeStatus $Status "正在安装 Microsoft WebView2 Runtime"
        $Installer = Start-Process `
            -FilePath $InstallerPath `
            -ArgumentList @("/silent", "/install") `
            -Wait `
            -PassThru
        if ($Installer.ExitCode -ne 0) {
            throw "WebView2 installer exited with code $($Installer.ExitCode)."
        }
    }
    finally {
        Remove-Item -LiteralPath $InstallerPath -Force -ErrorAction SilentlyContinue
    }

    for ($Attempt = 0; $Attempt -lt 120; $Attempt++) {
        $InstalledVersion = Get-MabobotWebView2Version
        if ($InstalledVersion) {
            return $InstalledVersion
        }
        Start-Sleep -Milliseconds 500
    }
    throw "WebView2 installation completed, but the runtime was not detected."
}

Export-ModuleMember -Function @(
    "Get-MabobotCompatiblePython",
    "Get-MabobotPythonInfo",
    "Get-MabobotWebView2Version",
    "Test-MabobotWebView2Runtime",
    "Install-MabobotPython",
    "Install-MabobotWebView2Runtime"
)
