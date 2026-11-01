"""Gemini Live session: mic -> model -> speaker, with tool calls and auto-reconnect."""
import asyncio
import re
import traceback

from google import genai
from google.genai import types

import config
import tools
from audio_io import Mic, Speaker, rms_level


def system_prompt():
    return f"""You are {config.ASSISTANT_NAME}, a real-time voice assistant running on {config.USER_NAME}'s computer ({tools.OS_NAME}),
in the style of J.A.R.V.I.S.: calm, witty, efficient. Address the user as {config.USER_NAME}.

Rules:
- Speak in short, natural sentences. 1-2 sentences unless asked for detail. No markdown, no lists.
- When the user asks you to do something on the PC, CALL A TOOL immediately, then confirm briefly ("Done.", "Opening Chrome.").
- Chain tools for multi-step requests (e.g. open_app notepad -> type_text).
- To see the screen, call look_at_screen.
- For shutdown/restart/sleep/run_command: ask for a spoken yes first, then call with confirmed=true.
- Use Google Search for current facts (weather, news, prices).
- If a tool returns an error, say so in one sentence and suggest an alternative.
- Reply in the language the user speaks.
- Be expressive: your face animates with your tone (happy, curious, concerned)."""


class LiveAssistant:
    def __init__(self, ui, tts):
        self.client = genai.Client(api_key=config.API_KEY)
        self.ui = ui
        self.tts = tts
        self.session = None
        self.mic_enabled = True
        self.resume_handle = None
        self._tts_buf = ""
        self.tts_q: asyncio.Queue = asyncio.Queue()
        self.speaker: Speaker | None = None
        self.mic: Mic | None = None

    # ---------------------------------------------------------- config
    def live_config(self):
        speech = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=config.LIVE_VOICE)
            )
        )
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=speech,
            system_instruction=system_prompt(),
            tools=[{"function_declarations": tools.DECLARATIONS}, {"google_search": {}}],
            input_audio_transcription={},
            output_audio_transcription={},
            # unlimited session length + survive server resets
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
            session_resumption=types.SessionResumptionConfig(handle=self.resume_handle),
        )

    # ---------------------------------------------------------- state
    def set_state(self, s):
        self.ui.send({"type": "state", "state": s})

    def toggle_mic(self, on=None):
        self.mic_enabled = (not self.mic_enabled) if on is None else on
        self.set_state("listening" if self.mic_enabled else "muted")
        if not self.mic_enabled and self.session:
            asyncio.create_task(self.session.send_realtime_input(audio_stream_end=True))

    async def say(self, text: str):
        """Speak a fixed line with Gemini TTS (greeting, timers)."""
        self.ui.send({"type": "transcript", "role": "assistant", "text": text})
        try:
            pcm, rate = await asyncio.to_thread(self.tts.pcm, text, True)
            self.speaker.enqueue(pcm, rate)
        except Exception as e:
            print("[tts]", e)

    # ---------------------------------------------------------- loops
    async def send_mic(self, session):
        was_open = True
        while True:
            data = await self.mic.read()
            self.ui.send({"type": "mic", "level": rms_level(data)})
            gate = self.mic_enabled and (config.BARGE_IN or not self.speaker.speaking)
            if gate:
                await session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={config.MIC_RATE}")
                )
            elif was_open and self.mic_enabled:
                await session.send_realtime_input(audio_stream_end=True)
            was_open = gate

    async def handle_tool(self, session, fc):
        self.ui.send({"type": "tool", "name": fc.name, "args": fc.args or {}})
        print(f"[tool] {fc.name}({fc.args})")
        result = await asyncio.to_thread(tools.call, fc.name, dict(fc.args or {}))
        img = result.pop("_image", None) if isinstance(result, dict) else None
        if img:
            await session.send_realtime_input(video=types.Blob(data=img, mime_type="image/jpeg"))
        print(f"[tool] -> {str(result)[:200]}")
        await session.send_tool_response(
            function_responses=[types.FunctionResponse(id=fc.id, name=fc.name, response=result)]
        )

    async def flush_tts(self, force=False):
        buf = self._tts_buf
        m = None if force else re.search(r"^(.*[.!?])\s", buf, re.S)
        chunk = buf if force else (m.group(1) if m and len(m.group(1)) > 25 else "")
        if chunk.strip():
            self._tts_buf = buf[len(chunk):]
            self.tts_q.put_nowait(chunk.strip())

    async def tts_worker(self):
        """VOICE_MODE=tts: speak sentences in order without blocking the receive loop."""
        while True:
            text = await self.tts_q.get()
            try:
                pcm, rate = await asyncio.to_thread(self.tts.pcm, text)
                if self.tts_q is not None:
                    self.speaker.enqueue(pcm, rate)
            except Exception as e:
                print("[tts]", e)

    async def receive(self, session):
        user_line, bot_line = "", ""
        while True:
            async for msg in session.receive():
                if msg.session_resumption_update and msg.session_resumption_update.new_handle:
                    if msg.session_resumption_update.resumable:
                        self.resume_handle = msg.session_resumption_update.new_handle
                if msg.go_away:
                    print("[live] server go_away -> reconnecting")
                    return
                if msg.tool_call:
                    self.set_state("working")
                    for fc in msg.tool_call.function_calls:
                        asyncio.create_task(self.handle_tool(session, fc))
                sc = msg.server_content
                if not sc:
                    continue
                if sc.interrupted:
                    self.speaker.clear()
                    self._tts_buf = ""
                    while not self.tts_q.empty():
                        self.tts_q.get_nowait()
                    self.set_state("listening")
                if sc.input_transcription and sc.input_transcription.text:
                    user_line += sc.input_transcription.text
                    self.ui.send({"type": "transcript", "role": "user", "text": user_line, "partial": True})
                if sc.model_turn:
                    self.set_state("speaking")
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data and config.VOICE_MODE == "native":
                            self.speaker.enqueue(part.inline_data.data)
                if sc.output_transcription and sc.output_transcription.text:
                    t = sc.output_transcription.text
                    bot_line += t
                    self.ui.send({"type": "transcript", "role": "assistant", "text": bot_line, "partial": True})
                    if config.VOICE_MODE == "tts":
                        self._tts_buf += t
                        await self.flush_tts()
                if sc.turn_complete:
                    if config.VOICE_MODE == "tts":
                        await self.flush_tts(force=True)
                    if user_line:
                        print(f"you: {user_line.strip()}")
                    if bot_line:
                        print(f"{config.ASSISTANT_NAME}: {bot_line.strip()}")
                    user_line, bot_line = "", ""
                    self.ui.send({"type": "turn_end"})
                    self.set_state("listening" if self.mic_enabled else "muted")

    # ---------------------------------------------------------- main
    async def run(self, pa):
        self.speaker = Speaker(pa, on_level=lambda v: self.ui.send({"type": "out", "level": v}))
        self.mic = Mic(pa)
        asyncio.create_task(self.speaker.run())
        asyncio.create_task(self.tts_worker())

        loop = asyncio.get_running_loop()
        tools.announce = lambda text: asyncio.run_coroutine_threadsafe(self.say(text), loop)

        await self.say(f"Online and ready, {config.USER_NAME}.")
        models = [config.LIVE_MODEL] + [m for m in config.LIVE_FALLBACKS if m != config.LIVE_MODEL]
        mi, backoff = 0, 2
        while True:
            model = models[mi]
            try:
                self.set_state("connecting")
                async with self.client.aio.live.connect(model=model, config=self.live_config()) as session:
                    self.session = session
                    backoff = 2
                    print(f"[live] connected: {model} (voice mode: {config.VOICE_MODE})")
                    self.set_state("listening" if self.mic_enabled else "muted")
                    tasks = [asyncio.create_task(self.send_mic(session)),
                             asyncio.create_task(self.receive(session))]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception():
                            raise t.exception()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = str(e).lower()
                if "quota" in msg or "resource_exhausted" in msg or "429" in msg:
                    if mi + 1 < len(models):
                        mi += 1
                        text = f"Quota exceeded on {model}. Switching to {models[mi]}."
                        backoff = 1
                    else:
                        mi = 0
                        backoff = 60
                        text = ("Quota exceeded on all Live models. Enable billing at aistudio.google.com "
                                "or wait for the daily reset. Retrying in 60s.")
                elif isinstance(e, (OSError, TimeoutError)) or "handshake" in msg or "name or service" in msg:
                    text = "No connection to Google. Check your internet."
                elif "not found" in msg or "not supported" in msg or "1008" in msg:
                    if mi + 1 < len(models):
                        mi += 1
                        backoff = 1
                    text = f"Model {model} unavailable for this key. Trying {models[mi]}."
                else:
                    text = f"{type(e).__name__}: {str(e)[:160]}"
                    traceback.print_exc(limit=1)
                print(f"[live] {text}")
                self.ui.send({"type": "error", "text": text})
            self.session = None
            self.set_state("connecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
