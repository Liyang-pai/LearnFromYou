import asyncio
import sys
from .config import TTS_VOICE
from .windows_speech import start_windows_speech


class Speaker:
    def __init__(self):
        self.process = None
        self.lock = asyncio.Lock()

    async def stop(self):
        async with self.lock:
            process = self.process
            self.process = None
            if process and process.returncode is None:
                process.terminate()
                await process.wait()

    async def speak(self, text):
        await self.stop()
        async with self.lock:
            if sys.platform == "win32":
                self.process = await start_windows_speech(text, TTS_VOICE)
            else:
                self.process = await asyncio.create_subprocess_exec(
                    "/usr/bin/say", "-v", TTS_VOICE, "-r", "185", "--", text,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            process = self.process
        code = await process.wait()
        # Windows termination returns 1; intentional interruption is not a TTS failure.
        return code if self.process is process else -15
