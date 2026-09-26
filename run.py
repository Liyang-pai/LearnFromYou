"""Start with: .venv/bin/python run.py"""
import uvicorn
from backend.config import PORT

if __name__ == "__main__":
    uvicorn.run("backend.app:app", host="127.0.0.1", port=PORT, access_log=False)

