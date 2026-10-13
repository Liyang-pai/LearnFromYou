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

function Show-InstallerDownloadProgress {
    param([long]$Received, [long]$Total, [double]$Seconds, [switch]$Finished)
    $receivedMB = $Received / 1MB
    $speed = $receivedMB / [Math]::Max($Seconds, 0.001)
    if ($Total -gt 0) {
        $percent = [Math]::Min(99, [Math]::Floor(100.0 * $Received / $Total))
        $status = '{0}% | {1:N2} / {2:N2} MB | {3:N2} MB/s' -f $percent, $receivedMB, ($Total / 1MB), $speed
    } else {
        $percent = -1
        $frame = @('|', '/', '-', '\')[[int]($Seconds * 5) % 4]
        $status = '[{0}] {1:N2} MB downloaded | {2:N2} MB/s | {3:N0}s' -f $frame, $receivedMB, $speed, $Seconds
    }
    if ($Finished) {
        $percent = 100
        $status = '100% | {0:N2} MB downloaded' -f $receivedMB
    }
    Write-Progress -Id 1 -Activity "Downloading Python $PythonVersion" -Status $status -PercentComplete $percent
}

function Receive-InstallerDownload {
    param([string]$Uri, [string]$Destination)
    # HttpClient streams the response; polling async operations keeps the console
    # moving even during connection/read waits. The deadline covers the whole file.
    Add-Type -AssemblyName System.Net.Http
    $ProgressPreference = 'Continue'
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromSeconds(180)
    $cancel = New-Object System.Threading.CancellationTokenSource
    $cancel.CancelAfter(180000)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $response = $null
    $source = $null
    $destinationStream = $null
    $received = 0L
    $total = -1L
    try {
        $request = $client.GetAsync([uri]$Uri, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token)
        while (-not $request.IsCompleted) {
            $cancel.Token.ThrowIfCancellationRequested()
            Show-InstallerDownloadProgress $received $total $clock.Elapsed.TotalSeconds
            Start-Sleep -Milliseconds 200
        }
        $response = $request.GetAwaiter().GetResult()
        $null = $response.EnsureSuccessStatusCode()
        if ($null -ne $response.Content.Headers.ContentLength) {
            $total = [long]$response.Content.Headers.ContentLength
        }
        $source = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $destinationStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
        $buffer = New-Object byte[] 65536
        $lastUpdate = -1.0
        while ($true) {
            $cancel.Token.ThrowIfCancellationRequested()
            $read = $source.ReadAsync($buffer, 0, $buffer.Length, $cancel.Token)
            while (-not $read.IsCompleted) {
                $cancel.Token.ThrowIfCancellationRequested()
                Show-InstallerDownloadProgress $received $total $clock.Elapsed.TotalSeconds
                Start-Sleep -Milliseconds 200
            }
            $count = $read.GetAwaiter().GetResult()
            if ($count -eq 0) { break }
            $destinationStream.Write($buffer, 0, $count)
            $received += $count
            if ($clock.Elapsed.TotalSeconds - $lastUpdate -ge 0.2) {
                Show-InstallerDownloadProgress $received $total $clock.Elapsed.TotalSeconds
                $lastUpdate = $clock.Elapsed.TotalSeconds
            }
        }
        if ($total -ge 0 -and $received -ne $total) { throw 'Python installer download is incomplete.' }
        $destinationStream.Flush()
        Show-InstallerDownloadProgress $received $total $clock.Elapsed.TotalSeconds -Finished
        Write-Host ('Download complete: {0:N2} MB. Checking SHA-256 and signature...' -f ($received / 1MB))
    } finally {
        Write-Progress -Id 1 -Activity 'Downloading Python' -Completed
        if ($destinationStream) { $destinationStream.Dispose() }
        if ($source) { $source.Dispose() }
        if ($response) { $response.Dispose() }
        $client.Dispose()
        $cancel.Dispose()
        $clock.Stop()
    }
}

function Invoke-PythonInstaller {
    param([string]$Installer, [string]$LogPath)
    $arguments = @('/quiet', '/norestart', '/log', ('"' + $LogPath + '"'),
        'InstallAllUsers=0', ('TargetDir="' + $RuntimeDirectory + '"'),
        'PrependPath=0', 'AppendPath=0', 'Include_pip=1', 'Include_launcher=0',
        'InstallLauncherAllUsers=0', 'Include_test=0', 'Include_doc=0',
        'Include_tcltk=0', 'Include_freethreaded=0', 'AssociateFiles=0', 'Shortcuts=0')
    $ProgressPreference = 'Continue'
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        $process = Start-Process -FilePath $Installer -ArgumentList $arguments -PassThru
        do {
            $frame = @('|', '/', '-', '\')[[int]($clock.Elapsed.TotalSeconds * 5) % 4]
            $status = '[{0}] Installing... {1:N0}s elapsed. Keep this window open.' -f $frame, $clock.Elapsed.TotalSeconds
            Write-Progress -Id 2 -Activity 'Installing Python for the current user' -Status $status -PercentComplete -1
        } while (-not $process.WaitForExit(200))
    } finally {
        Write-Progress -Id 2 -Activity 'Installing Python' -Completed
        $clock.Stop()
    }
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
        if (Test-Path -LiteralPath $installer) {
            try { Assert-Installer $installer }
            catch { Remove-Item -LiteralPath $installer -Force }
        }
        if (-not (Test-Path -LiteralPath $installer)) {
            Receive-InstallerDownload -Uri $InstallerUrl -Destination $partial
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
    # Inherit the console so Python can render live activity and pip progress.
    # Piping through Out-Host would turn stdout into a line-buffered pipe.
    $quotedArguments = ($arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $process = Start-Process -FilePath $python -ArgumentList $quotedArguments -NoNewWindow -Wait -PassThru
    return $process.ExitCode
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
