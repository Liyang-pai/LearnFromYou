"""Local service settings. ASR selection is managed in the model UI."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
API_KEY = os.getenv("DEEPSEEK_API_KEY") or os.getenv("Deepseek_API", "")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
MODELS_DIR = ROOT / "asr/models"
ASR_THREADS = int(os.getenv("ASR_THREADS", "2"))
SEGMENT_PAUSE = float(os.getenv("SEGMENT_PAUSE", "3"))
LONG_PAUSE = float(os.getenv("LONG_SEGMENT_PAUSE", "1"))
SEGMENT_MAX_WAIT = float(os.getenv("SEGMENT_MAX_WAIT", "20"))
SPEAK_PAUSE = float(os.getenv("SPEAK_PAUSE", "5"))
DIRECT_PAUSE = float(os.getenv("DIRECT_PAUSE", "1"))
ASR_REVIEW_GLOSSARY_TIMEOUT = float(os.getenv("ASR_REVIEW_GLOSSARY_TIMEOUT", "30"))
ASR_REVIEW_TIMEOUT = float(os.getenv("ASR_REVIEW_TIMEOUT", "15"))
TTS_VOICE = os.getenv("TTS_VOICE", "Tingting")
PORT = int(os.getenv("PORT", "8765"))
