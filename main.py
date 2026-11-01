"""Entry point: UI server + Gemini Live assistant."""
import asyncio
import json
import shutil
import subprocess
import sys
import webbrowser

import pyaudio
from aiohttp import web
from google import genai

import config
from live_session import LiveAssistant
from tts import GeminiTTS


class UI:
    """Tiny websocket hub that pushes state/levels/transcripts to the face UI."""

    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()
        self.on_command = lambda msg: None
        self.loop = None

    def send(self, msg: dict):
        data = json.dumps(msg)
        if self.loop is None:
            raise RuntimeError("UI server loop is not running")
        future = asyncio.run_coroutine_threadsafe(self._broadcast(data), self.loop)
        future.add_done_callback(self._broadcast_done)

    async def _broadcast(self, data):
        for ws in list(self.clients):
            if ws.closed:
                self.clients.discard(ws)
                continue
            try:
                await ws.send_str(data)
            except (ConnectionError, RuntimeError) as e:
                self.clients.discard(ws)
                print("[ui] websocket send failed:", e)

    @staticmethod
    def _broadcast_done(future):
        if future.cancelled():
            return
        error = future.exception()
        if error:
            print("[ui] broadcast failed:", error)

    async def ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.add(ws)
        ws_msg = {"type": "hello", "name": config.ASSISTANT_NAME, "user": config.USER_NAME}
        await ws.send_str(json.dumps(ws_msg))
        async for m in ws:
            if m.type == web.WSMsgType.TEXT:
                self.on_command(json.loads(m.data))
        self.clients.discard(ws)
        return ws

    async def toggle_handler(self, request):
        self.on_command({"type": "toggle_mic"})
        return web.Response(text="ok")

    async def start(self):
        self.loop = asyncio.get_running_loop()
        app = web.Application()
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/", lambda r: web.FileResponse(config.ROOT / "ui" / "index.html"))
        # for desktop shortcuts: curl -X POST http://127.0.0.1:8765/toggle
        app.router.add_post("/toggle", self.toggle_handler)
        app.router.add_get("/toggle", self.toggle_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", config.UI_PORT).start()


def open_window(url):
    """Open the UI as a frameless app window (Edge/Chrome), else default browser."""
    for exe in ("msedge", "chrome", "google-chrome", "chromium-browser", "chromium"):
        path = shutil.which(exe)
        if not path and sys.platform == "win32" and exe == "msedge":
            path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        if path:
            try:
                subprocess.Popen([path, f"--app={url}", "--window-size=1100,800"])
                return
            except Exception:
                pass
    webbrowser.open(url)


async def main():
    needs_key = config.MODE == "live" or "gemini" in [config.LLM_BACKEND] or config.TTS_ENGINE == "gemini"
    if needs_key and not config.API_KEY:
        sys.exit("Set GEMINI_API_KEY in .env (get one at https://aistudio.google.com/apikey)")

    ui = UI()
    await ui.start()
    url = f"http://127.0.0.1:{config.UI_PORT}"
    print(f"[ui] {url}")
    open_window(url)

    if config.MODE == "live":
        bot = LiveAssistant(ui, GeminiTTS(genai.Client(api_key=config.API_KEY)))
    else:
        from assistant_lite import LiteAssistant
        bot = LiteAssistant(ui)
    print(f"[mode] {config.MODE}")

    def on_cmd(msg):
        if msg.get("type") == "toggle_mic":
            bot.toggle_mic()
        elif msg.get("type") == "text" and msg.get("text"):
            if config.MODE == "live":
                if bot.session:
                    asyncio.ensure_future(bot.session.send_realtime_input(text=msg["text"]))
            else:
                bot.text_command(msg["text"])
    ui.on_command = on_cmd

    loop = asyncio.get_running_loop()
    if sys.platform == "win32":
        try:
            import keyboard  # global hotkey
            keyboard.add_hotkey(config.HOTKEY, lambda: loop.call_soon_threadsafe(bot.toggle_mic))
            print(f"[hotkey] {config.HOTKEY} toggles the mic")
        except Exception as e:
            print("[hotkey] disabled:", e)
    else:
        print(f"[hotkey] bind a desktop shortcut to: curl -s -X POST {url}/toggle")

    pa = pyaudio.PyAudio()
    try:
        await bot.run(pa)
    finally:
        pa.terminate()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("bye")
