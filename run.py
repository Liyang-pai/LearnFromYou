"""Start with: .venv/bin/python run.py [--open-browser]."""
import argparse
from pathlib import Path
from threading import Event, Thread
import webbrowser

import uvicorn
from backend.config import PORT


def open_when_ready(server, stopped):
    while not stopped.wait(0.1):
        if server.started:
            try:
                webbrowser.open(f'http://127.0.0.1:{PORT}')
            except OSError:
                print(f'Open http://127.0.0.1:{PORT} in your browser.', flush=True)
            return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args()
    print('启动代码目录：' + str(Path(__file__).resolve().parent), flush=True)
    print('按 Ctrl+C 结束服务。', flush=True)
    server = uvicorn.Server(uvicorn.Config("backend.app:app", host="127.0.0.1", port=PORT, access_log=False))
    stopped = Event()
    if args.open_browser:
        Thread(target=open_when_ready, args=(server, stopped), daemon=True).start()
    try:
        server.run()
    finally:
        stopped.set()

