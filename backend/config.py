"""Local service settings. ASR selection is managed in the model UI."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
API_KEY = os.getenv("DEEPSEEK_API_KEY") or os.getenv("Deepseek_API", "")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
IMAGE_MODEL = os.getenv("LLM_IMAGE_MODEL", "deepseek-flash")
DATA_ROOT = Path(os.environ['LEARNFROMYOU_DATA_ROOT']) if os.getenv('LEARNFROMYOU_DATA_ROOT') else None
SETTINGS_PATH = Path(os.environ['LEARNFROMYOU_SETTINGS_PATH']) if os.getenv('LEARNFROMYOU_SETTINGS_PATH') else None
MODELS_DIR = Path(os.getenv('ASR_MODELS_DIR', ROOT / 'asr/models'))


def data_root():
    """Keep existing installs and ROOT-monkeypatched tests compatible."""
    return Path(DATA_ROOT) if DATA_ROOT is not None else ROOT


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
