import asyncio
import base64
import json
import subprocess

from backend import speech, windows_speech


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.finished = asyncio.Event()

    def terminate(self):
        self.returncode = -15
        self.finished.set()

    async def wait(self):
        await self.finished.wait()
        return self.returncode


def test_windows_text_is_data_and_window_is_hidden(monkeypatch):
    text = '中文、引号 " 和换行\n$(throw "不能执行")'
    captured = {}

    class Input:
        def write(self, data):
            captured['data'] = data

        async def drain(self):
            pass

        def close(self):
            captured['closed'] = True

    async def create(*args, **kwargs):
        captured['args'], captured['kwargs'] = args, kwargs
        process = FakeProcess()
        process.stdin = Input()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    monkeypatch.setattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
    asyncio.run(windows_speech.start_windows_speech(text, 'Tingting'))
    assert json.loads(base64.b64decode(captured['data'])) == {'text': text, 'voice': 'Tingting'}
    assert base64.b64decode(captured['args'][-1]).decode('utf-16-le') == windows_speech.SCRIPT
    assert captured['kwargs']['creationflags'] == 0x08000000
    assert captured['closed']


def test_windows_speaker_can_be_stopped(monkeypatch):
    async def run():
        process = FakeProcess()

        def terminate():
            process.returncode = 1  # Windows TerminateProcess exit code.
            process.finished.set()

        process.terminate = terminate

        async def start(text, voice):
            return process

        monkeypatch.setattr(speech.sys, 'platform', 'win32')
        monkeypatch.setattr(speech, 'start_windows_speech', start)
        speaker = speech.Speaker()
        task = asyncio.create_task(speaker.speak('测试'))
        while speaker.process is None:
            await asyncio.sleep(0)
        await speaker.stop()
        assert await task == -15
        assert speaker.process is None

    asyncio.run(run())


def test_macos_keeps_say_command(monkeypatch):
    captured = []

    async def create(*args, **kwargs):
        captured.extend(args)
        process = FakeProcess()
        process.returncode = 0
        process.finished.set()
        return process

    monkeypatch.setattr(speech.sys, 'platform', 'darwin')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    assert asyncio.run(speech.Speaker().speak('中文测试')) == 0
    assert captured[0] == '/usr/bin/say'
    assert captured[-2:] == ['--', '中文测试']
