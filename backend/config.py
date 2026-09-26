"""All model choices and timing knobs live here, not in the UI."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
API_KEY = os.getenv("DEEPSEEK_API_KEY") or os.getenv("Deepseek_API", "")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
MODEL_DIR = Path(os.getenv("ASR_MODEL_DIR", str(ROOT / "asr/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09")))
VAD_PATH = ROOT / "asr/models/silero_vad.onnx"
ASR_THREADS = int(os.getenv("ASR_THREADS", "2"))
SEGMENT_PAUSE = float(os.getenv("SEGMENT_PAUSE", "3"))
LONG_PAUSE = float(os.getenv("LONG_SEGMENT_PAUSE", "1"))
SEGMENT_MAX_WAIT = float(os.getenv("SEGMENT_MAX_WAIT", "20"))
SPEAK_PAUSE = float(os.getenv("SPEAK_PAUSE", "5"))
DIRECT_PAUSE = float(os.getenv("DIRECT_PAUSE", "1"))
TTS_VOICE = os.getenv("TTS_VOICE", "Tingting")
PORT = int(os.getenv("PORT", "8765"))

