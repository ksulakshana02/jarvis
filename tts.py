"""Gemini TTS: text -> WAV file (cached) -> PCM for playback."""
import hashlib
import io
import wave

from google import genai

import config


def _pcm_to_wav(pcm: bytes, rate=24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(wav_bytes: bytes):
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return w.readframes(w.getnframes()), w.getframerate()


class GeminiTTS:
    def __init__(self, client: genai.Client):
        self.client = client
        config.TTS_CACHE.mkdir(exist_ok=True)

    def _request(self, text: str, voice_cfg: dict):
        return self.client.models.generate_content(
            model=config.TTS_MODEL,
            contents=[{
                "role": "user",
                "parts": [{"text": text, "speech_metadata": {"style": config.TTS_STYLE}}],
            }],
            config={"response_modalities": ["AUDIO"], "speech_config": {"voice_config": voice_cfg}},
        )

    def synthesize(self, text: str) -> bytes:
        """Returns WAV bytes. 3.8 TTS models return WAV; older ones return raw 24k PCM."""
        try:
            resp = self._request(text, {"voice": config.TTS_VOICE})
        except Exception:
            # older TTS models use prebuilt_voice_config and no speech_metadata
            resp = self.client.models.generate_content(
                model=config.TTS_MODEL,
                contents=f"Say in a {config.TTS_STYLE} tone: {text}",
                config={
                    "response_modalities": ["AUDIO"],
                    "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": config.TTS_VOICE}}},
                },
            )
        data = resp.candidates[0].content.parts[0].inline_data.data
        return data if data[:4] == b"RIFF" else _pcm_to_wav(data)

    def to_file(self, text: str, cache=True):
        """Generate (or reuse) a WAV file for this text. Returns Path."""
        key = hashlib.sha1(f"{config.TTS_MODEL}|{config.TTS_VOICE}|{config.TTS_STYLE}|{text}".encode()).hexdigest()[:16]
        path = config.TTS_CACHE / f"{key}.wav"
        if not (cache and path.exists()):
            path.write_bytes(self.synthesize(text))
        return path

    def pcm(self, text: str, cache=False):
        """(pcm_bytes, sample_rate) ready for Speaker.enqueue."""
        if cache:
            return wav_to_pcm(self.to_file(text).read_bytes())
        return wav_to_pcm(self.synthesize(text))


class EdgeTTS:
    """Microsoft Edge neural voices (free, online). Has Sinhala: si-LK-SameeraNeural / si-LK-ThiliniNeural."""
    RATE = 24000

    def __init__(self):
        import edge_tts  # noqa: F401  (fail early if not installed)
        config.TTS_CACHE.mkdir(exist_ok=True)

    def _mp3(self, text: str) -> bytes:
        import asyncio
        import edge_tts

        async def run():
            c = edge_tts.Communicate(text, config.EDGE_VOICE, rate=config.EDGE_RATE)
            return b"".join([ch["data"] async for ch in c.stream() if ch["type"] == "audio"])
        return asyncio.run(run())  # called from a worker thread, so a fresh loop is fine

    def _decode(self, mp3: bytes) -> bytes:
        import av
        out = bytearray()
        with av.open(io.BytesIO(mp3)) as f:
            rs = av.AudioResampler(format="s16", layout="mono", rate=self.RATE)
            for frame in f.decode(audio=0):
                for r in rs.resample(frame):
                    out += r.to_ndarray().tobytes()
            for r in rs.resample(None):
                out += r.to_ndarray().tobytes()
        return bytes(out)

    def to_file(self, text: str):
        key = hashlib.sha1(f"edge|{config.EDGE_VOICE}|{config.EDGE_RATE}|{text}".encode()).hexdigest()[:16]
        path = config.TTS_CACHE / f"edge_{key}.wav"
        if not path.exists():
            for attempt in range(3):  # Edge sometimes answers "No audio was received"; a retry usually works
                try:
                    mp3 = self._mp3(text)
                    if mp3:
                        break
                except Exception as e:
                    if attempt == 2:
                        raise
                    print(f"[tts] edge: {str(e)[:80]} -> retry")
            path.write_bytes(_pcm_to_wav(self._decode(mp3), self.RATE))
        return path

    def pcm(self, text: str):
        return wav_to_pcm(self.to_file(text).read_bytes())


if __name__ == "__main__":
    import sys
    t = GeminiTTS(genai.Client(api_key=config.API_KEY))
    p = t.to_file(" ".join(sys.argv[1:]) or f"Hello, I am {config.ASSISTANT_NAME}. All systems online.")
    print("saved", p)
