"""Windows local TTS via the installed System.Speech voices."""
import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path


SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$synth = $null
try {
    Add-Type -AssemblyName System.Speech
    $encoded = [Console]::In.ReadToEnd()
    $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))
    $payload = ConvertFrom-Json -InputObject $json
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $voices = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled })
    $chosen = $voices | Where-Object { $_.VoiceInfo.Name -eq $payload.voice } | Select-Object -First 1
    if (-not $chosen) {
        $chosen = $voices | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh-*' } | Select-Object -First 1
    }
    if ($chosen) { $synth.SelectVoice($chosen.VoiceInfo.Name) }
    $synth.SetOutputToDefaultAudioDevice()
    $synth.Speak([string]$payload.text)
} catch {
    exit 1
} finally {
    if ($synth) { $synth.Dispose() }
}
"""


async def start_windows_speech(text: str, voice: str):
    """Pass classroom text as data, never interpolate it into PowerShell code."""
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii")
    process = await asyncio.create_subprocess_exec(
        str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    payload = base64.b64encode(json.dumps({"text": text, "voice": voice}, ensure_ascii=False).encode("utf-8"))
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
    except BaseException:
        if process.returncode is None:
            process.terminate()
        await process.wait()
        raise
    return process
