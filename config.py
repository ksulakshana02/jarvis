import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def _bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


API_KEY = os.getenv("GEMINI_API_KEY", "")
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Jarvis")
USER_NAME = os.getenv("USER_NAME", "sir")

# lite = wake word + local speech-to-text + Gemini text + local voice (~1 API call per command)
# live = Gemini Live real-time streaming (best experience, uses a lot of quota)
MODE = os.getenv("MODE", "lite").lower()

# lite mode brain: ollama (local) | openai (any OpenAI-compatible URL) | gemini
LLM_BACKEND = os.getenv("LLM_BACKEND", "ollama").lower()
LLM_FALLBACK = [b.strip() for b in os.getenv("LLM_FALLBACK", "gemini").split(",") if b.strip()]
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
WARMUP_BRAIN = _bool("WARMUP_BRAIN", False)
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "45"))  # seconds before falling back to the next brain
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:1234/v1")  # LM Studio default
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "local-model")
LLM_VISION = _bool("LLM_VISION", False)  # true if your local model can see images (e.g. gemma3:4b)
TEXT_MODEL = os.getenv("TEXT_MODEL", "gemini-3.5-flash-lite")
TEXT_FALLBACKS = [m.strip() for m in os.getenv(
    "TEXT_FALLBACKS", "gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-3.5-flash").split(",") if m.strip()]
WAKE_WORD = os.getenv("WAKE_WORD", "hey_jarvis")  # bundled: hey_jarvis, alexa, hey_mycroft, hey_marvin
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base.en")  # tiny.en (fastest), base.en, small.en, small (multilingual)
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en")
# whisper = local, English only in practice | gemini = understands Sinhala + English mixed (1 small call per command)
STT_ENGINE = os.getenv("STT_ENGINE", "whisper").lower()
STT_MODELS = [m.strip() for m in os.getenv("STT_MODELS", "gemini-3.5-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
TTS_ENGINE = os.getenv("TTS_ENGINE", "piper")  # piper (local, free) | edge (free, online, has Sinhala) | gemini (uses quota)
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_GB-alan-medium")
EDGE_VOICE = os.getenv("EDGE_VOICE", "si-LK-SameeraNeural")  # TTS_ENGINE=edge. Female: si-LK-ThiliniNeural
EDGE_RATE = os.getenv("EDGE_RATE", "+0%")                     # speed, e.g. +10% / -10%
REPLY_LANGUAGE = os.getenv("REPLY_LANGUAGE", "en").lower()    # en | si (Jarvis answers in Sinhala)
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "16"))
FOLLOWUP_SECONDS = float(os.getenv("FOLLOWUP_SECONDS", "6"))
END_SILENCE_MS = int(os.getenv("END_SILENCE_MS", "800"))
VAD_MIN_RMS = float(os.getenv("VAD_MIN_RMS", "400"))

# Live (real-time conversation)
LIVE_MODEL = os.getenv("LIVE_MODEL", "gemini-3.8-live")
# tried in order when a model returns "quota exceeded"
LIVE_FALLBACKS = [m.strip() for m in os.getenv(
    "LIVE_FALLBACKS", "gemini-3.1-flash-live-preview,gemini-2.5-flash-native-audio-preview-12-2025").split(",") if m.strip()]
LIVE_VOICE = os.getenv("LIVE_VOICE", "Charon")

# native = model speaks directly (lowest latency)
# tts    = model text -> Gemini TTS model speaks it (custom voice/style, more latency)
VOICE_MODE = os.getenv("VOICE_MODE", "native").lower()

# TTS (used for greeting, timers/announcements, and VOICE_MODE=tts)
TTS_MODEL = os.getenv("TTS_MODEL", "gemini-3.8-flash-lite-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "Charon")
TTS_STYLE = os.getenv("TTS_STYLE", "calm, confident, slightly British butler")
TTS_CACHE = ROOT / "voice_cache"

# Audio
MIC_RATE = 16000
SPK_RATE = 24000
CHUNK = 512
# false = mic muted while assistant talks (use with speakers, avoids echo)
# true  = you can interrupt anytime (use headphones)
BARGE_IN = _bool("BARGE_IN", False)

# Safety
ALLOW_SHELL = _bool("ALLOW_SHELL", False)

# UI
UI_PORT = int(os.getenv("UI_PORT", "8765"))
HOTKEY = os.getenv("HOTKEY", "ctrl+alt+j")  # toggle mic
