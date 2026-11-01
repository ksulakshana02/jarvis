# Jarvis: real-time voice assistant that controls your PC

Talk naturally. Gemini Live hears you, answers out loud in real time, and calls tools to control the computer.
The face lip-syncs to the voice and changes expression.

## Brain options (lite mode)
| `LLM_BACKEND=` | What | Cost |
|---|---|---|
| `ollama` (default) | Local LLM, e.g. `qwen3:4b`. Fully offline. | Free, unlimited |
| `openai` | Any OpenAI-compatible server: LM Studio, llama.cpp, Groq, OpenRouter | Local = free; cloud = their limits |
| `gemini` | Gemini Flash-Lite | Free tier quota |

With `ollama` + `TTS_ENGINE=piper`, Jarvis runs **100% offline**. No API key needed.
Speed on CPU: `qwen3:1.7b` ≈ instant, `qwen3:4b` ≈ 1–3 s per answer, `llama3.1:8b` needs a GPU to feel fast.
Change model: `ollama pull qwen3:1.7b` then set `OLLAMA_MODEL=qwen3:1.7b`.

## Two modes
| `MODE=` | How it works | Gemini quota use |
|---|---|---|
| `lite` (default) | Say **"Hey Jarvis"** → local Whisper → brain (local Ollama LLM or Gemini) + tools → local Piper voice | None with Ollama |
| `live` | Streams mic to Gemini Live continuously, real-time talk-back | Heavy, hits free quota fast |

Lite mode: wake word, speech-to-text and voice all run on your CPU and cost nothing. After it answers you have 6 s to say a follow-up without the wake word. The mic button = "listen now".
First run downloads Whisper `base.en` (~140 MB) and the Piper voice (~60 MB).

## Run on Rocky Linux (9 / 10)

```bash
unzip jarvis.zip && cd jarvis
chmod +x setup_rocky.sh run.sh
./setup_rocky.sh          # one time: packages, venv, ydotool, Ollama + qwen3:4b
nano .env                 # optional: GEMINI_API_KEY (only for fallback / live mode)
./run.sh                  # starts Jarvis + opens the face window
```

**Check your session type:** `echo $XDG_SESSION_TYPE`
- `x11`: everything works (keyboard via xdotool/pyautogui, screenshots via mss).
- `wayland`: keyboard/typing works through `ydotool` (the setup script installs it and starts a `ydotoold` service). Screenshots use `gnome-screenshot`.
  If keys don't work: `systemctl status ydotoold`. Or on Rocky 9 log out, click the gear icon and pick **GNOME on Xorg**.

**Mic toggle hotkey (GNOME):** Settings → Keyboard → Custom Shortcuts → add:
- Command: `curl -s -X POST http://127.0.0.1:8765/toggle`
- Shortcut: `Ctrl+Alt+J`

**Mic not picked up?** Choose the input in GNOME Settings → Sound. pyaudio uses the system default (PipeWire).

## Run on Windows
Double-click `run.bat`. It creates the venv and opens `.env`. Add your key, then run `run.bat` again. Ctrl+Alt+J toggles the mic.

## Controls
- Mic button / hotkey: mute or unmute (face goes grey and sleepy)
- `/` in the window: type a command instead of speaking
- Speakers: keep `BARGE_IN=false` (mic pauses while Jarvis talks, so it doesn't hear itself)
- Headphones: set `BARGE_IN=true` to interrupt mid-sentence

## Face
| State | Face |
|---|---|
| Talking | Mouth opens with voice volume; mouth shape changes per syllable (A / E / O / M); head nods |
| Happy ("Done!", "Sure") | Smile, cheeks up, eyes squint |
| Concerned ("Sorry", "error") | Frown, inner brows down |
| Curious (questions) | Brows up (one higher), head tilt |
| Thinking (running a tool) | Amber, eyes glance up-right |
| Listening (you speak) | Brows lifted, eyes wide, glow follows your voice |
| Muted | Grey, half-closed eyes |
| Always | Random blinks, eye movements, breathing, follows your mouse |

Emotion is read from Jarvis's own words, so it costs no extra API calls. Tune it in `ui/index.html` (`EMO`, `detectEmotion`).

## What you can say
"Open Firefox / VS Code / Files / Terminal" · "Close Chrome" · "Play lofi on YouTube" · "Volume 40 / mute" ·
"Pause / next song" · "Brightness 70" · "Open text editor and write: buy milk" · "Press ctrl+s" ·
"What's on my screen?" · "Find my resume in Downloads" · "Timer 10 minutes for tea" · "Battery?" ·
"Lock the PC" · "Shut down" (asks first) · "Weather in Colombo?" (Google Search) ·
"Run `df -h`" (only with `ALLOW_SHELL=true`, asks first)

## Voice
- `VOICE_MODE=native` (default): the Live model's own voice (`LIVE_VOICE`). Fastest.
- `VOICE_MODE=tts`: each sentence is re-voiced by Gemini TTS (`TTS_VOICE`, `TTS_STYLE`). Custom style, ~1 s slower.
- The greeting and timers always use TTS, cached as WAV files in `voice_cache/`. `python tts.py "Good evening."` makes one.

## Add a tool
In `tools.py`, write a function and add `_fn(...)` to `DECLARATIONS`. It registers automatically.

## Troubleshooting
- `pyaudio` build fails: `sudo dnf install portaudio-devel python3.12-devel gcc`, then `pip install pyaudio`
- Lite mode doesn't wake: lower `WAKE_THRESHOLD=0.35`, or click the mic button. Keeps listening forever: raise `VAD_MIN_RMS`.
- Model name rejected: in `.env` set `LIVE_MODEL=gemini-3.1-flash-live-preview` or `TTS_MODEL=gemini-3.1-flash-tts-preview`
- Brightness needs the `video` group, so log out and back in once after setup
