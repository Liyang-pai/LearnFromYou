# Windows PowerShell 5.1 compatible. All messages are ASCII to avoid code-page
# differences before Python takes over the UTF-8 console.
[CmdletBinding()]
param([switch]$PrepareOnly)

$ErrorActionPreference = 'Stop'
$PythonVersion = '3.13.16'
$InstallerUrl = 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-amd64.exe'
# Official SHA-256: https://www.python.org/downloads/release/python-31316/
$InstallerSha256 = 'fb4f9f5d438b2396da0086dc70b935c530cb578e37adc6d354f7ad2037fee83b'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDirectory = Join-Path $env:LOCALAPPDATA 'LearnFromYou\Python313'

function Invoke-PythonProbe {
    param([string]$Executable, [string[]]$Prefix = @(), [string]$ExpectedVenv = '')
    # WindowsApps contains Store aliases, which can open a Store window.
    if ($Executable -match '[\\/]WindowsApps[\\/]') { return $null }
    $arguments = @($Prefix) + @('-I', '-X', 'utf8', (Join-Path $ProjectRoot 'scripts\bootstrap.py'), '--probe')
    if ($ExpectedVenv) { $arguments += @('--venv', $ExpectedVenv) }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $info.Arguments = ($arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(15000)) {
            $process.Kill()
            $process.WaitForExit()
            return $null
        }
        if ($process.ExitCode -eq 0) { return $stdout.Result.Trim() }
        return $null
    } catch { return $null }
    finally { $process.Dispose() }
}

function Find-CompatiblePython {
    $venv = Join-Path $ProjectRoot '.venv'
    $python = Invoke-PythonProbe -Executable (Join-Path $venv 'Scripts\python.exe') -ExpectedVenv $venv
    if ($python) { return $python }
    $python = Invoke-PythonProbe -Executable (Join-Path $RuntimeDirectory 'python.exe')
    if ($python) { return $python }
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($launcher) {
        # Disable optional auto-install by Python launchers; only our verified
        # installer below may install a missing interpreter.
        $env:PYLAUNCHER_ALLOW_INSTALL = ''
        $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = 'false'
        foreach ($version in @('3.13', '3.12', '3.11')) {
            $python = Invoke-PythonProbe -Executable $launcher.Source -Prefix @("-$version")
            if ($python) { return $python }
        }
    }
    foreach ($name in @('python.exe', 'python3.exe')) {
        foreach ($command in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
            if ($command) {
                $python = Invoke-PythonProbe -Executable $command.Source
                if ($python) { return $python }
            }
        }
    }
    # An official installation need not be on PATH or have a py launcher.
    foreach ($version in @('3.13', '3.12', '3.11')) {
        foreach ($hive in @('HKEY_CURRENT_USER', 'HKEY_LOCAL_MACHINE')) {
            $key = "Registry::${hive}\Software\Python\PythonCore\$version\InstallPath"
            if (Test-Path -LiteralPath $key) {
                $installed = Get-Item -LiteralPath $key
                $path = $installed.GetValue('ExecutablePath')
                if (-not $path) {
                    $directory = $installed.GetValue('')
                    if (-not $directory) { continue }
                    $path = Join-Path $directory 'python.exe'
                }
                $python = Invoke-PythonProbe -Executable $path
                if ($python) { return $python }
            }
        }
    }
    return $null
}

function Assert-Installer {
    param([string]$Path)
    $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($hash -ne $InstallerSha256) { throw 'Python installer SHA-256 does not match the official release.' }
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.Subject -notmatch '(?:^|,\s*)O=Python Software Foundation(?:,|$)') {
        throw 'Python installer must have a valid Python Software Foundation digital signature.'
    }
}

function Invoke-PythonInstaller {
    param([string]$Installer, [string]$LogPath)
    $arguments = @('/quiet', '/norestart', '/log', ('"' + $LogPath + '"'),
        'InstallAllUsers=0', ('TargetDir="' + $RuntimeDirectory + '"'),
        'PrependPath=0', 'AppendPath=0', 'Include_pip=1', 'Include_launcher=0',
        'InstallLauncherAllUsers=0', 'Include_test=0', 'Include_doc=0',
        'Include_tcltk=0', 'Include_freethreaded=0', 'AssociateFiles=0', 'Shortcuts=0')
    $process = Start-Process -FilePath $Installer -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -in @(3010, 1641)) {
        throw 'Python installation requires a Windows restart. Restart manually, then launch again.'
    }
    if ($process.ExitCode -ne 0) {
        throw "Python installation failed (exit $($process.ExitCode)). See $LogPath"
    }
}

function Install-ManagedPython {
    Write-Host "No compatible Python found. Downloading official Python $PythonVersion x64..."
    $cache = Join-Path $env:LOCALAPPDATA 'LearnFromYou\downloads'
    $null = New-Item -ItemType Directory -Path $cache -Force
    $installer = Join-Path $cache "python-$PythonVersion-amd64.exe"
    # Unique partial files avoid accidentally executing an interrupted download.
    $partial = Join-Path $cache ([guid]::NewGuid().ToString() + '.part.exe')
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = 'SilentlyContinue'
        if (Test-Path -LiteralPath $installer) {
            try { Assert-Installer $installer }
            catch { Remove-Item -LiteralPath $installer -Force }
        }
        if (-not (Test-Path -LiteralPath $installer)) {
            Invoke-WebRequest -Uri $InstallerUrl -OutFile $partial -UseBasicParsing -TimeoutSec 180
            Assert-Installer $partial
            Move-Item -LiteralPath $partial -Destination $installer -Force
        }
        Write-Host 'Installing Python for the current user. Please keep this window open...'
        $log = Join-Path $cache ('install-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
        Invoke-PythonInstaller -Installer $installer -LogPath $log
        $python = Invoke-PythonProbe -Executable (Join-Path $RuntimeDirectory 'python.exe')
        if (-not $python) { throw "Installed Python validation failed. See $log" }
        return $python
    } finally {
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    }
}

function Start-LearnFromYou {
    $architecture = $env:PROCESSOR_ARCHITEW6432
    if (-not $architecture) { $architecture = $env:PROCESSOR_ARCHITECTURE }
    if (-not [Environment]::Is64BitOperatingSystem -or $architecture -ne 'AMD64') {
        throw 'This launcher supports Windows x64 only.'
    }
    Set-Location -LiteralPath $ProjectRoot
    $env:PYTHONUTF8 = '1'
    Write-Host 'Checking Python (supported: CPython 3.11-3.13 x64)...'
    $python = Find-CompatiblePython
    if (-not $python) { $python = Install-ManagedPython }
    Write-Host "Using Python: $python"
    $arguments = @('-X', 'utf8', (Join-Path $ProjectRoot 'scripts\bootstrap.py'))
    if ($PrepareOnly) { $arguments += '--prepare-only' }
    & $python @arguments | Out-Host
    return $LASTEXITCODE
}

# Dot-source for offline Windows tests without starting/installing anything.
if ($MyInvocation.InvocationName -ne '.') {
    try { exit (Start-LearnFromYou) }
    catch {
        Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host 'Check network access and directory permissions, then double-click again.'
        Write-Host 'Manual Python download: https://www.python.org/downloads/windows/'
        exit 1
    }
}
