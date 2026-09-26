import asyncio
from .config import TTS_VOICE


class Speaker:
    def __init__(self):
        self.process = None
        self.lock = asyncio.Lock()

    async def stop(self):
        async with self.lock:
            if self.process and self.process.returncode is None:
                self.process.terminate()
                await self.process.wait()
            self.process = None

    async def speak(self, text):
        await self.stop()
        async with self.lock:
            self.process = await asyncio.create_subprocess_exec(
                "/usr/bin/say", "-v", TTS_VOICE, "-r", "185", "--", text,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            process = self.process
        await process.wait()
        return process.returncode
