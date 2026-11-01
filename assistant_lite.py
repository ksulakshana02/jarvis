"""Low-quota mode: everything local except ONE Gemini text call per command.

  mic ─► "Hey Jarvis" wake word (openWakeWord, local)
      ─► record until you stop talking (local)
      ─► speech-to-text (faster-whisper, local)
      ─► brain: local LLM via Ollama (default) or Gemini Flash-Lite, with tool calling
      ─► voice (Piper, local, saved as WAV)  ─► speaker + face lip-sync
"""
import asyncio
import os
import re
import time
import urllib.request
import wave
from collections import deque

import numpy as np
import pyaudio
import config
import tools
from audio_io import Speaker, rms_level
from brain import FallbackBrain

FRAME = 1280  # 80 ms @ 16 kHz (openWakeWord frame size)

# Fixed phrases Jarvis says without the LLM, per REPLY_LANGUAGE.
PHRASES = {
    "en": {"play": "Playing {q} on YouTube.", "yt_search": "Searching YouTube for {q}.",
           "search": "Searching for {q}.", "open": "Opening {q}.", "done": "Done.",
           "quota": "I've hit the API quota for today.", "offline": "Sorry, my brain is offline. Is Ollama running?",
           "online": "Online, {name}. Say {wake} when you need me."},
    "si": {"play": "යූටියුබ් එකේ {q} ප්ලේ කරනවා.", "yt_search": "යූටියුබ් එකේ {q} හොයනවා.",
           "search": "{q} ගැන හොයනවා.", "open": "{q} විවෘත කරනවා.", "done": "හරි, කළා.",
           "quota": "අද දවසට API සීමාව ඉවරයි.", "offline": "සමාවෙන්න, මගේ මොළය දැන් ක්‍රියා කරන්නේ නැහැ. Ollama දුවනවද?",
           "online": "ආයුබෝවන් {name}. මගේ උදව් ඕන නම් {wake} කියන්න."},
}


def say(key, **kw):
    return PHRASES.get(config.REPLY_LANGUAGE, PHRASES["en"])[key].format(**kw)


# ------------------------------------------------------------------ local engines
class WakeWord:
    def __init__(self):
        import openwakeword
        from openwakeword.model import Model
        d = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
        path = os.path.join(d, f"{config.WAKE_WORD}_v0.1.onnx")
        self.name = os.path.basename(path)[:-5]
        self.model = Model(wakeword_model_paths=[path])

    def hit(self, frame: bytes) -> bool:
        score = self.model.predict(np.frombuffer(frame, np.int16))[self.name]
        return score >= config.WAKE_THRESHOLD

    def reset(self):
        self.model.reset()


class STT:
    def __init__(self):
        self.model = None
        self.gemini = None
        if config.STT_ENGINE == "gemini" and config.API_KEY:
            from google import genai
            self.gemini = genai.Client(api_key=config.API_KEY)
            self.mi = 0
            print(f"[stt] gemini {config.STT_MODELS[0]} (Sinhala + English); whisper only if that fails")
        else:
            self._load_whisper()

    def _load_whisper(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            print(f"[stt] loading whisper '{config.WHISPER_MODEL}' (first run downloads it)")
            self.model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
        return self.model

    GEMINI_PROMPT = (
        "You are the speech-to-text for a voice assistant on a PC. The user speaks Sinhala, English, or a mix "
        "(commands like opening YouTube, playing songs, searching, opening apps). Transcribe exactly what is said. "
        "Write Sinhala words in Sinhala script; write English words, app, website and brand names "
        "(YouTube, Chrome, Google) in English. Output only the transcript, no quotes. "
        "If there is no clear speech, output nothing.")

    def _gemini(self, pcm: bytes) -> str:
        import io
        from google.genai import types
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm)
        audio = types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav")
        last = None
        for _ in range(len(config.STT_MODELS)):
            model = config.STT_MODELS[self.mi]
            try:
                r = self.gemini.models.generate_content(model=model, contents=[self.GEMINI_PROMPT, audio],
                                                        config=types.GenerateContentConfig(temperature=0))
                return (r.text or "").strip().strip('"')
            except Exception as e:
                last = e
                print(f"[stt] {model}: {str(e)[:90]} -> next model")
                self.mi = (self.mi + 1) % len(config.STT_MODELS)
        raise last

    def transcribe(self, pcm: bytes) -> str:
        if self.gemini:
            try:
                return self._gemini(pcm)
            except Exception as e:
                print("[stt] gemini failed, using local whisper:", str(e)[:120])
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
        segs, _ = self._load_whisper().transcribe(audio, language=config.STT_LANGUAGE or None, beam_size=1,
                                                  vad_filter=True, condition_on_previous_text=False)
        return " ".join(s.text for s in segs).strip()


class PiperTTS:
    BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

    def __init__(self):
        from piper import PiperVoice
        vdir = config.ROOT / "voices"
        vdir.mkdir(exist_ok=True)
        name = config.PIPER_VOICE  # e.g. en_GB-alan-medium
        lang, speaker, quality = name.split("-")
        model = vdir / f"{name}.onnx"
        for suffix in (".onnx", ".onnx.json"):
            f = vdir / f"{name}{suffix}"
            if not f.exists():
                url = f"{self.BASE}/{lang.split('_')[0]}/{lang}/{speaker}/{quality}/{name}{suffix}"
                print(f"[tts] downloading {url}")
                urllib.request.urlretrieve(url, f)
        self.voice = PiperVoice.load(model)
        config.TTS_CACHE.mkdir(exist_ok=True)

    def to_file(self, text: str):
        import hashlib
        path = config.TTS_CACHE / f"piper_{hashlib.sha1((config.PIPER_VOICE + text).encode()).hexdigest()[:16]}.wav"
        if not path.exists():
            with wave.open(str(path), "wb") as wf:
                self.voice.synthesize_wav(text, wf)
        return path

    def pcm(self, text: str):
        with wave.open(str(self.to_file(text))) as w:
            return w.readframes(w.getnframes()), w.getframerate()


# ------------------------------------------------------------------ assistant
class LiteAssistant:
    def __init__(self, ui):
        self.ui = ui
        self.brain = FallbackBrain(on_tool=self._on_tool)
        self.state = "standby"
        self.force_listen = False
        self.busy = False
        self.speaker = None
        self.tts = None

    # ---------------------------------------------------------- ui
    def set_state(self, s):
        self.state = s
        self.ui.send({"type": "state", "state": s})

    def say_ui(self, role, text):
        self.ui.send({"type": "transcript", "role": role, "text": text})
        self.ui.send({"type": "turn_end"})

    def _on_tool(self, name, args):
        self.set_state("working")
        self.ui.send({"type": "tool", "name": name, "args": args})

    def toggle_mic(self, on=None):
        """Mic button / hotkey: wake up now, or cancel."""
        if self.state in ("standby", "connecting"):
            self.force_listen = True
        else:
            self.speaker.clear()
            self.set_state("standby")

    def text_command(self, text):
        if not self.busy:
            asyncio.ensure_future(self.handle(text))

    async def speak(self, text):
        if not text:
            return
        # one sentence at a time: the first one starts playing while the rest are still being made
        for part in [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]:
            try:
                pcm, rate = await asyncio.to_thread(self.tts.pcm, part)
                self.speaker.enqueue(pcm, rate)
            except Exception as e:
                print("[tts]", e)

    # ---------------------------------------------------------- brain
    _LEAD = r"(?:hey|ok|okay|jarvis|please|bro|now|can you|could you|would you|i want you to|just|you)"
    _TAIL = r"(?:please|bro|dude|man|now|for me|jarvis)"  # no "you" here: "play shape of you"
    FILLER = re.compile(rf"^(?:{_LEAD}\s+)+|(?:\s+{_TAIL})+$")  # only at the start/end, never inside a song name

    def _match_direct(self, text):
        """Unambiguous browser commands -> (tool, args, reply), or None to ask the LLM."""
        # strip punctuation only: [^\w] would also delete Sinhala vowel signs
        t = re.sub(r"[,!?;:\"'()\[\]“”‘’]", " ", text.lower()).replace("you tube", "youtube").rstrip(". ")
        if re.search("[\u0D80-\u0DFF]", t):
            return self._match_sinhala(t)
        t = self.FILLER.sub("", " ".join(t.split()))
        # "play X", "open youtube and play X song", "play X on youtube"
        if m := re.fullmatch(r"(?:(?:open|go to|launch|start) youtube (?:and |then )?)?(?:play|put on) (?:the )?"
                             r"(.+?)(?: song| video| music)?(?: on youtube)?", t):
            return "play_youtube", {"query": m[1]}, say("play", q=m[1])
        if m := re.fullmatch(r"search (?:for )?(.+?) on youtube|search youtube (?:for )?(.+)", t):
            q = m[1] or m[2]
            return "search_web", {"query": q, "site": "youtube"}, say("yt_search", q=q)
        if m := re.fullmatch(r"(?:search|google)(?: google)? (?:for )?(.+)", t):
            return "search_web", {"query": m[1], "site": "google"}, say("search", q=m[1])
        if m := re.fullmatch(r"(?:open|go to|launch|start|show me)(?: the)? (youtube|[\w-]+(?:\.[\w-]+)+)(?: website| site)?", t):
            site = m[1]
            url = "https://www.youtube.com" if site == "youtube" else site
            return "open_website", {"url": url}, say("open", q="YouTube" if site == "youtube" else site)
        return None

    # Sinhala / Singlish commands, e.g. "YouTube එකේ අපි කවුරුද සින්දුව play කරන්න", "යූටියුබ් open කරන්න"
    SI_FILLER = re.compile(r"^(?:(?:අනේ|ජාර්විස්|jarvis|මචං|බං|පොඩ්ඩක්|ඉක්මනට|hey)\s+)+"
                           r"|(?:\s+(?:අනේ|මචං|බං|පොඩ්ඩක්|ඉක්මනට|ප්ලීස්|please))+$")
    SI_YT = r"(?:youtube|යූටියුබ්|යුටියුබ්)"
    SI_DO = r"(?:කරන්න|කරන්නේ|කරපන්|කරන්නකෝ|කරනවද|කරන්නද|කරලා දෙන්න|කරලා දෙන්නකෝ|karanna)"
    SI_PLAY = rf"(?:(?:play|ප්ලේ|පලේ|ප්ලෙ|පල|වාදනය)\s*{SI_DO}|දාන්න|දාපන්|දාන්නකෝ|දාලා දෙන්න)"
    SI_OPEN = rf"(?:(?:open|ඕපන්|විවෘත)\s*{SI_DO}|අරින්න|අරපන්|අරින්නකෝ)"
    SI_IN = r"(?:\s*(?:එකෙන්|එකේ|එකට|ඒකේ|එක)(?=\s|$))?"  # longest first
    SI_SONG = r"(?:\s*(?:සින්දුව|සිංදුව|සින්දු|සිංදු|ගීතය|ගීතේ|song|video|එක))?"
    SI_SEARCH = rf"(?:හොයන්න|හොයපන්|හොයලා දෙන්න|search\s*{SI_DO}|සර්ච්\s*{SI_DO})"

    def _match_sinhala(self, t):
        t = self.SI_FILLER.sub("", " ".join(t.split()))
        yt, _in = self.SI_YT, self.SI_IN
        # [youtube එකේ] [youtube open කරලා] X [සින්දුව] play කරන්න
        if m := re.fullmatch(rf"(?:{yt}{_in}\s*(?:(?:open|ඕපන්|විවෘත)\s*කරලා\s*)?(?:ගිහින්|ගිහිල්ලා|ගිහින්\s+ඒකේ)?\s*)?(.+?){self.SI_SONG}\s*{self.SI_PLAY}", t):
            q = m[1].strip()
            if not q or re.fullmatch(yt, q):  # "youtube play කරන්න" -> just open YouTube
                return "open_website", {"url": "https://www.youtube.com"}, say("open", q="YouTube")
            return "play_youtube", {"query": q}, say("play", q=q)
        # youtube එකේ X හොයන්න
        if m := re.fullmatch(rf"{yt}{_in}\s*(.+?)\s*(?:ගැන\s*)?{self.SI_SEARCH}", t):
            return "search_web", {"query": m[1], "site": "youtube"}, say("yt_search", q=m[1])
        # google එකේ X හොයන්න / X google කරන්න
        if m := (re.fullmatch(rf"(?:google|ගූගල්){_in}\s*(.+?)\s*(?:ගැන\s*)?{self.SI_SEARCH}", t)
                 or re.fullmatch(rf"(.+?)\s*(?:ගැන\s*)?(?:google|ගූගල්)\s*{self.SI_DO}", t)):
            return "search_web", {"query": m[1], "site": "google"}, say("search", q=m[1])
        # youtube [එක] open කරන්න / github.com open කරන්න
        if m := re.fullmatch(rf"({yt}|[\w-]+(?:\.[\w-]+)+){_in}\s*{self.SI_OPEN}", t):
            is_yt = bool(re.fullmatch(yt, m[1]))
            url = "https://www.youtube.com" if is_yt else m[1]
            return "open_website", {"url": url}, say("open", q="YouTube" if is_yt else m[1])
        return None

    async def _direct_command(self, text):
        """Unambiguous browser commands skip the LLM. Returns (reply, tool_task) or None."""
        hit = self._match_direct(text)
        if not hit:
            return None
        name, args, reply = hit
        self._on_tool(name, args)
        print(f"[direct] {name}({args})")
        # run the tool in the background so Jarvis can start talking at the same time
        return reply, asyncio.ensure_future(asyncio.to_thread(tools.call, name, args))

    async def handle(self, text: str):
        self.busy = True
        self.say_ui("user", text)
        print(f"you: {text}")
        self.set_state("thinking")
        tool_task = None
        try:
            direct = await self._direct_command(text)
            if direct:
                reply, tool_task = direct
            else:
                reply = await asyncio.to_thread(self.brain.chat, text)
        except Exception as e:
            msg = str(e)
            reply = (say("quota") if "quota" in msg.lower() or "429" in msg else say("offline"))
            print("[brain] error:", msg[:300])
            self.ui.send({"type": "error", "text": msg[:160]})
        reply = reply or say("done")
        print(f"{config.ASSISTANT_NAME}: {reply}")
        self.say_ui("assistant", reply)
        self.set_state("speaking")
        await self.speak(reply)
        if tool_task:
            result = await tool_task
            if isinstance(result, dict) and result.get("error"):
                print("[direct] error:", result["error"])
                self.ui.send({"type": "error", "text": str(result["error"])[:160]})
        self.busy = False

    # ---------------------------------------------------------- ears
    async def run(self, pa: pyaudio.PyAudio):
        self.speaker = Speaker(pa, on_level=lambda v: self.ui.send({"type": "out", "level": v}))
        asyncio.create_task(self.speaker.run())
        loop = asyncio.get_running_loop()
        tools.announce = lambda text: asyncio.run_coroutine_threadsafe(self.speak(text), loop)

        self.set_state("connecting")
        if config.TTS_ENGINE == "edge":
            from tts import EdgeTTS
            self.tts = EdgeTTS()
        elif config.TTS_ENGINE == "gemini":
            from tts import GeminiTTS
            from google import genai
            self.tts = GeminiTTS(genai.Client(api_key=config.API_KEY))
        else:
            self.tts = await asyncio.to_thread(PiperTTS)
        wake = await asyncio.to_thread(WakeWord)
        stt = await asyncio.to_thread(STT)
        if config.WARMUP_BRAIN:
            print("[brain] warming up (set WARMUP_BRAIN=false to skip)")
            await asyncio.to_thread(self.brain.warmup)
        mic = pa.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=FRAME)

        wake_name = config.WAKE_WORD.replace("_", " ").title()
        print(f"[ready] say '{wake_name}' (or click the mic). Brain: {config.LLM_BACKEND} -> fallback {config.LLM_FALLBACK}")
        self.set_state("standby")
        await self.speak(say("online", name=config.USER_NAME, wake=wake_name))

        noise = 300.0
        pre = deque(maxlen=4)  # keep 320 ms before speech starts
        rec, started, silent_ms, total_ms, follow_until = bytearray(), False, 0, 0, 0.0

        while True:
            frame = await asyncio.to_thread(mic.read, FRAME, exception_on_overflow=False)
            lvl = rms_level(frame)
            r = lvl * 6000
            self.ui.send({"type": "mic", "level": lvl if self.state == "listening" else 0})

            if self.state == "speaking" and not self.speaker.speaking and not self.busy:
                follow_until = time.time() + config.FOLLOWUP_SECONDS  # can answer without wake word
                self.set_state("listening")
                rec, started, silent_ms, total_ms = bytearray(), False, 0, 0
                wake.reset()
                continue

            if self.state in ("standby", "speaking"):
                noise = 0.95 * noise + 0.05 * r
                if self.speaker.speaking and not config.BARGE_IN and not self.force_listen:
                    continue  # don't let its own voice trigger the wake word
                woke = self.force_listen or (await asyncio.to_thread(wake.hit, frame))
                if woke and not self.busy:
                    self.force_listen = False
                    self.speaker.clear()
                    wake.reset()
                    self.set_state("listening")
                    rec, started, silent_ms, total_ms = bytearray(), False, 0, 0
                    follow_until = time.time() + 6
                continue

            if self.state != "listening":
                continue

            # --- record one utterance (simple adaptive energy VAD)
            speech = r > max(noise * 2.5, config.VAD_MIN_RMS)
            if not started:
                pre.append(frame)
                if speech:
                    started = True
                    rec = bytearray(b"".join(pre))
                elif time.time() > follow_until:
                    self.set_state("standby")
                continue
            rec += frame
            total_ms += 80
            silent_ms = 0 if speech else silent_ms + 80
            if silent_ms >= config.END_SILENCE_MS or total_ms >= 15000:
                started = False
                self.set_state("thinking")
                text = await asyncio.to_thread(stt.transcribe, bytes(rec))
                if len(text) < 2 or text.lower().strip(" .!?") in ("you", ""):
                    self.set_state("standby")
                    continue
                await self.handle(text)
