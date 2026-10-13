# Offline tests for the real Windows launcher functions. No downloads/installs.
$ErrorActionPreference = 'Stop'
$Launcher = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\start_windows.ps1'

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Assert-Fails([scriptblock]$Action, [string]$Pattern) {
    try { & $Action }
    catch {
        if ($_.Exception.Message -match $Pattern) { return }
        throw
    }
    throw "Expected failure: $Pattern"
}

& {
    . $Launcher
    $script:ProbeCalls = @()
    function Invoke-PythonProbe {
        param($Executable, $Prefix, $ExpectedVenv)
        $script:ProbeCalls += $Executable
        return 'existing-venv-python.exe'
    }
    Assert-True ((Find-CompatiblePython) -eq 'existing-venv-python.exe') 'Must prefer existing venv'
    Assert-True ($script:ProbeCalls.Count -eq 1) 'Do not inspect/install system Python if venv works'
}

& {
    . $Launcher
    function Get-Command { return $null }
    function Test-Path { return $false }
    function Invoke-PythonProbe { return $null }
    Assert-True ($null -eq (Find-CompatiblePython)) 'Missing/incompatible Python must request installation'
}

& {
    . $Launcher
    function Get-Command {
        param($Name)
        if ($Name -eq 'py.exe') { return [pscustomobject]@{ Source = 'py.exe' } }
        return $null
    }
    $script:Versions = @()
    function Invoke-PythonProbe {
        param($Executable, $Prefix, $ExpectedVenv)
        if ($Executable -eq 'py.exe') {
            $script:Versions += $Prefix[0]
            if ($Prefix[0] -eq '-3.12') { return 'python312.exe' }
        }
        return $null
    }
    Assert-True ((Find-CompatiblePython) -eq 'python312.exe') 'Must accept Python 3.12'
    Assert-True (($script:Versions -join ',') -eq '-3.13,-3.12') 'Prefer latest supported version'
}

& {
    . $Launcher
    Assert-True ($null -eq (Invoke-PythonProbe 'C:\Users\someone\AppData\Local\Microsoft\WindowsApps\python.exe')) `
        'Must skip Store aliases without executing them'
}

& {
    . $Launcher
    function Get-FileHash { return [pscustomobject]@{ Hash = 'wrong-hash' } }
    function Get-AuthenticodeSignature { throw 'Should not inspect signature after hash failure' }
    Assert-Fails { Assert-Installer 'fake.exe' } 'SHA-256'
}

& {
    . $Launcher
    function Get-FileHash { return [pscustomobject]@{ Hash = $InstallerSha256 } }
    function Get-AuthenticodeSignature {
        return [pscustomobject]@{ Status = 'NotSigned'; SignerCertificate = $null }
    }
    Assert-Fails { Assert-Installer 'fake.exe' } 'digital signature'
    function Get-AuthenticodeSignature {
        return [pscustomobject]@{ Status = 'Valid'; SignerCertificate = @{ Subject = 'CN=Other,O=Other' } }
    }
    Assert-Fails { Assert-Installer 'fake.exe' } 'digital signature'
    function Get-AuthenticodeSignature {
        return [pscustomobject]@{ Status = 'Valid'; SignerCertificate = @{ Subject = 'CN=PSF, O=Python Software Foundation, C=US' } }
    }
    Assert-Installer 'fake.exe'
}

& {
    . $Launcher
    $RuntimeDirectory = 'C:\Users\Test User\Python313'
    $script:InstallerExit = 0
    function Start-Process {
        param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru)
        Assert-True ($ArgumentList -contains 'InstallAllUsers=0') 'Install only for current user'
        Assert-True ($ArgumentList -contains 'PrependPath=0') 'Do not modify PATH'
        Assert-True ($ArgumentList -contains 'Include_pip=1') 'Include pip'
        Assert-True ($ArgumentList -contains 'Include_launcher=0') 'Do not install launcher'
        Assert-True ($ArgumentList -contains '/norestart') 'Do not restart automatically'
        Assert-True ($ArgumentList -contains 'TargetDir="C:\Users\Test User\Python313"') 'Quote target directory'
        $process = [pscustomobject]@{ ExitCode = $script:InstallerExit }
        $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($Timeout) return $true }
        return $process
    }
    Invoke-PythonInstaller 'fake.exe' 'fake.log'
    foreach ($code in @(3010, 1641)) {
        $script:InstallerExit = $code
        Assert-Fails { Invoke-PythonInstaller 'fake.exe' 'fake.log' } 'restart'
    }
    $script:InstallerExit = 1603
    Assert-Fails { Invoke-PythonInstaller 'fake.exe' 'fake.log' } '1603'
}

& {
    . $Launcher
    $cacheRoot = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
    $env:LOCALAPPDATA = $cacheRoot
    function Receive-InstallerDownload {
        param($Uri, $Destination)
        [IO.File]::WriteAllText($Destination, 'partial download')
        throw 'download timeout'
    }
    function Invoke-PythonInstaller { throw 'Must not execute incomplete installer' }
    try {
        Assert-Fails { Install-ManagedPython } 'download timeout'
        $remaining = @(Get-ChildItem -LiteralPath (Join-Path $cacheRoot 'LearnFromYou\downloads'))
        Assert-True ($remaining.Count -eq 0) 'Failed download must be removed'
    } finally { Remove-Item -LiteralPath $cacheRoot -Recurse -Force }
}

& {
    . $Launcher
    $script:Progress = @()
    function Write-Progress {
        param($Id, $Activity, $Status, $PercentComplete, [switch]$Completed)
        $script:Progress += [pscustomobject]@{ Status = $Status; Percent = $PercentComplete }
    }
    Show-InstallerDownloadProgress 1048576 4194304 2
    Assert-True ($script:Progress[-1].Percent -eq 25) 'Use actual downloaded bytes for percentage'
    Assert-True ($script:Progress[-1].Status -match 'MB/s') 'Show download speed'
    Show-InstallerDownloadProgress 4194304 4194304 2
    Assert-True ($script:Progress[-1].Percent -eq 99) 'Do not show 100% before successful EOF'
    Show-InstallerDownloadProgress 4194304 4194304 2 -Finished
    Assert-True ($script:Progress[-1].Percent -eq 100) 'Show completion after EOF'
    Show-InstallerDownloadProgress 1048576 -1 2
    Assert-True ($script:Progress[-1].Percent -eq -1) 'Unknown size must not invent a percentage'
    Assert-True ($script:Progress[-1].Status -match 'downloaded') 'Unknown size still shows received bytes'
}

Write-Host 'Offline Windows bootstrap tests passed.'
