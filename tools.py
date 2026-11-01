"""PC control tools exposed to Gemini via function calling.
Works on Windows and Linux (Rocky/RHEL/Fedora, GNOME on X11 or Wayland)."""
import configparser
import datetime
import difflib
import io
import os
import platform
import shlex
import shutil
import subprocess
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import config

IS_WIN = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
WAYLAND = IS_LINUX and (os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY")))
HOME = Path.home()
os.environ.setdefault("YDOTOOL_SOCKET", "/tmp/.ydotool_socket")

# hook set by live_session.py
announce = lambda text: print("[announce]", text)  # noqa: E731

# ================================================================ helpers

def _have(b):
    return shutil.which(b) is not None


def _run(cmd, check=True, timeout=10):
    """Run a command list; returns stdout. Raises on failure if check."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"{cmd[0]} failed").strip()[:200])
    return r.stdout.strip()


def _spawn(cmd):
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _open_path(p):
    if IS_WIN:
        os.startfile(str(p))
    else:
        _spawn(["xdg-open", str(p)])


def _com_init():
    if IS_WIN:
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass


# ---- keyboard: pyautogui (Windows / X11) or ydotool (Wayland)

# Linux input-event keycodes for ydotool
KEYCODES = {
    "esc": 1, "escape": 1, "backspace": 14, "tab": 15, "enter": 28, "return": 28, "ctrl": 29, "control": 29,
    "shift": 42, "alt": 56, "space": 57, "capslock": 58, "win": 125, "super": 125, "meta": 125,
    "up": 103, "down": 108, "left": 105, "right": 106, "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
    "insert": 110, "delete": 111, "del": 111, "printscreen": 99, "print": 99,
    "volumeup": 115, "volumedown": 114, "volumemute": 113, "playpause": 164, "nexttrack": 163,
    "prevtrack": 165, "stop": 166, "minus": 12, "-": 12, "equal": 13, "=": 13, "/": 53, ".": 52, ",": 51,
}
for i, k in enumerate("1234567890"):
    KEYCODES[k] = 2 + i
for row, start in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
    for i, k in enumerate(row):
        KEYCODES[k] = start + i
for i in range(1, 11):
    KEYCODES[f"f{i}"] = 58 + i
KEYCODES.update({"f11": 87, "f12": 88})

ALIASES = {"windows": "win", "cmd": "win", "command": "win", "option": "alt", "esc": "esc",
           "pgup": "pageup", "pgdn": "pagedown", "prtsc": "printscreen"}


def _norm_keys(keys):
    return [ALIASES.get(k, k) for k in (x.strip().lower() for x in keys) if k]


def _hotkey(*keys):
    keys = _norm_keys(keys)
    if WAYLAND:
        if not _have("ydotool"):
            raise RuntimeError("Wayland needs ydotool (run setup_rocky.sh) or log in with 'GNOME on Xorg'")
        codes = [KEYCODES[k] for k in keys]
        seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        _run(["ydotool", "key", *seq])
    else:
        import pyautogui
        pyautogui.hotkey(*keys)


def _press(key, times=1):
    for _ in range(max(1, times)):
        _hotkey(key)


def _type(text):
    if WAYLAND:
        _run(["ydotool", "type", "--key-delay", "4", "--", text], timeout=60)
    elif IS_LINUX and _have("xdotool"):
        _run(["xdotool", "type", "--delay", "4", "--", text], timeout=60)
    else:
        import pyperclip
        old = pyperclip.paste()
        pyperclip.copy(text)  # unicode-safe
        _hotkey("ctrl", "v")
        threading.Timer(0.5, lambda: pyperclip.copy(old)).start()


# ================================================================ apps

WIN_APPS = {
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "firefox": "firefox",
    "notepad": "notepad", "calculator": "calc", "paint": "mspaint", "file explorer": "explorer",
    "files": "explorer", "settings": "ms-settings:", "task manager": "taskmgr", "terminal": "wt",
    "cmd": "cmd", "powershell": "powershell", "vs code": "code", "vscode": "code",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "spotify": "spotify:",
    "whatsapp": "whatsapp:", "camera": "microsoft.windows.camera:", "store": "ms-windows-store:",
}
LINUX_APPS = {  # first binary found wins
    "chrome": ["google-chrome", "chromium-browser", "chromium"], "google chrome": ["google-chrome"],
    "chromium": ["chromium-browser", "chromium"], "firefox": ["firefox"], "browser": ["firefox", "google-chrome"],
    "files": ["nautilus"], "file manager": ["nautilus"], "file explorer": ["nautilus"],
    "terminal": ["ptyxis", "gnome-terminal", "konsole", "xterm"],
    "calculator": ["gnome-calculator"], "settings": ["gnome-control-center"],
    "text editor": ["gnome-text-editor", "gedit"], "notepad": ["gnome-text-editor", "gedit"],
    "system monitor": ["gnome-system-monitor"], "task manager": ["gnome-system-monitor"],
    "vs code": ["code"], "vscode": ["code"], "visual studio code": ["code"],
    "spotify": ["spotify"], "vlc": ["vlc"], "libreoffice": ["libreoffice"],
    "word": ["libreoffice --writer"], "excel": ["libreoffice --calc"], "powerpoint": ["libreoffice --impress"],
    "software": ["gnome-software"], "camera": ["snapshot", "cheese"], "screenshot": ["gnome-screenshot -i"],
}


def _desktop_entries():
    dirs = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
            HOME / ".local/share/applications", Path("/var/lib/flatpak/exports/share/applications"),
            HOME / ".local/share/flatpak/exports/share/applications"]
    out = {}
    for d in dirs:
        for f in d.glob("*.desktop") if d.exists() else []:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
                if e.get("NoDisplay", "false").lower() == "true":
                    continue
                out[e.get("Name", f.stem).lower()] = f
                out[f.stem.lower()] = f
            except Exception:
                pass
    return out


def _start_menu_shortcuts():
    roots = [Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
             Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs"]
    return {p.stem.lower(): p for r in roots if r.exists() for p in r.rglob("*.lnk")}


def _fuzzy(n, d):
    m = difflib.get_close_matches(n, d.keys(), n=1, cutoff=0.6) or [k for k in d if n in k][:1]
    return m[0] if m else None


def open_app(name: str):
    n = name.lower().strip()
    if n in ("youtube", "you tube"):
        return open_website("https://www.youtube.com")
    if IS_LINUX:
        for cand in LINUX_APPS.get(n, []):
            parts = shlex.split(cand)
            if _have(parts[0]):
                _spawn(parts)
                return f"Opened {name}."
        entries = _desktop_entries()
        key = _fuzzy(n, entries)
        if key:
            _spawn(["gio", "launch", str(entries[key])] if _have("gio") else ["gtk-launch", entries[key].stem])
            return f"Opened {key}."
        if _have(n.replace(" ", "-")):
            _spawn([n.replace(" ", "-")])
            return f"Opened {name}."
        return f"Couldn't find an app called {name}."
    if IS_WIN:
        target = WIN_APPS.get(n)
        if target:
            try:
                os.startfile(target) if target.endswith(":") else subprocess.Popen(f'start "" {target}', shell=True)
                return f"Opened {name}."
            except Exception:
                pass
        sc = _start_menu_shortcuts()
        key = _fuzzy(n, sc)
        if key:
            os.startfile(sc[key])
            return f"Opened {key}."
        import pyautogui
        _press("win"); pyautogui.sleep(0.6); pyautogui.write(name, interval=0.02); pyautogui.sleep(0.8); _press("enter")
        return f"Searched Start for '{name}' and launched the top result."
    _spawn(["open", "-a", name])
    return f"Opened {name}."


PROC_NAMES = {"chrome": ["chrome", "google-chrome", "chromium"], "vs code": ["code"], "vscode": ["code"],
              "files": ["nautilus"], "file manager": ["nautilus"], "word": ["winword", "soffice"],
              "excel": ["excel", "soffice"], "calculator": ["calculatorapp", "gnome-calculator"],
              "terminal": ["gnome-terminal-server", "ptyxis"], "edge": ["msedge"]}


def close_app(name: str):
    import psutil
    n = name.lower().strip()
    targets = PROC_NAMES.get(n, [n.replace(" ", "")])
    killed = 0
    for p in psutil.process_iter(["name"]):
        pn = (p.info["name"] or "").lower().removesuffix(".exe")
        if any(pn == t or (len(t) > 3 and pn.startswith(t)) for t in targets):
            try:
                p.terminate(); killed += 1
            except Exception:
                pass
    return f"Closed {name}." if killed else f"No running app matched {name}."


# ================================================================ web

def open_website(url: str):
    if url.lower().strip() in ("youtube", "you tube", "www.youtube.com"):
        url = "https://www.youtube.com"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opened {url}."


def search_web(query: str, site: str = "google"):
    q = urllib.parse.quote_plus(query)
    urls = {"google": f"https://www.google.com/search?q={q}",
            "youtube": f"https://www.youtube.com/results?search_query={q}",
            "maps": f"https://www.google.com/maps/search/{q}",
            "github": f"https://github.com/search?q={q}",
            "stackoverflow": f"https://stackoverflow.com/search?q={q}"}
    webbrowser.open(urls.get(site, urls["google"]))
    return f"Searched {site} for {query}."


def _first_youtube_video(query: str):
    """Return the watch URL of the top YouTube search result, or None."""
    import re
    import urllib.request
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception as e:
        print("[youtube] search failed:", e)
        return None
    m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"', html) or re.search(r'"videoId":"([\w-]{11})"', html)
    return f"https://www.youtube.com/watch?v={m[1]}" if m else None


def play_youtube(query: str):
    if not query.strip():
        return open_website("https://www.youtube.com")
    video = _first_youtube_video(query)
    if video:
        webbrowser.open(video)
        return f"Playing {query} on YouTube."
    webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
    return f"Couldn't pick a video, opened YouTube results for {query}."


# ================================================================ audio / media / display

def _linux_volume(action, value):
    sink = "@DEFAULT_AUDIO_SINK@"
    if _have("wpctl"):
        if action == "get":
            out = _run(["wpctl", "get-volume", sink])  # "Volume: 0.40 [MUTED]"
            return f"Volume is {round(float(out.split()[1]) * 100)}%{' (muted)' if 'MUTED' in out else ''}."
        if action in ("mute", "unmute"):
            _run(["wpctl", "set-mute", sink, "1" if action == "mute" else "0"]); return f"{action.title()}d."
        arg = {"set": f"{value}%", "up": f"{value}%+", "down": f"{value}%-"}[action]
        _run(["wpctl", "set-volume", "-l", "1.0", sink, arg]); return f"Volume {action} done."
    if _have("pactl"):
        s = "@DEFAULT_SINK@"
        if action == "get":
            return _run(["pactl", "get-sink-volume", s])
        if action in ("mute", "unmute"):
            _run(["pactl", "set-sink-mute", s, "1" if action == "mute" else "0"]); return f"{action.title()}d."
        arg = {"set": f"{value}%", "up": f"+{value}%", "down": f"-{value}%"}[action]
        _run(["pactl", "set-sink-volume", s, arg]); return f"Volume {action} done."
    raise RuntimeError("Install wireplumber (wpctl) or pulseaudio-utils (pactl)")


def _win_endpoint_volume():
    _com_init()
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    dev = AudioUtilities.GetSpeakers()
    ev = getattr(dev, "EndpointVolume", None)
    if ev is not None:
        return ev
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    return cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))


def volume(action: str, value: int = 10):
    if action == "set" and value is None:
        value = 50
    if IS_LINUX:
        return _linux_volume(action, value)
    try:
        ev = _win_endpoint_volume()
        cur = round(ev.GetMasterVolumeLevelScalar() * 100)
        if action in ("mute", "unmute"):
            ev.SetMute(1 if action == "mute" else 0, None); return f"{action.title()}d."
        if action == "get":
            return f"Volume is {cur}%."
        new = max(0, min(100, {"set": value, "up": cur + value, "down": cur - value}[action]))
        ev.SetMasterVolumeLevelScalar(new / 100, None)
        return f"Volume set to {new}%."
    except Exception:
        key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute", "unmute": "volumemute"}.get(action)
        _press(key, max(1, value // 2) if action in ("up", "down") else 1)
        return f"Volume {action}."


def media_control(action: str):
    if IS_LINUX and _have("playerctl"):
        cmd = {"play_pause": "play-pause", "next": "next", "previous": "previous", "stop": "stop"}.get(action, "play-pause")
        _run(["playerctl", cmd], check=False)
        return f"Media {action}."
    _press({"play_pause": "playpause", "next": "nexttrack", "previous": "prevtrack", "stop": "stop"}.get(action, "playpause"))
    return f"Media {action}."


def set_brightness(level: int):
    level = max(1, min(100, level))
    if IS_LINUX and _have("brightnessctl"):
        _run(["brightnessctl", "set", f"{level}%"])
    else:
        import screen_brightness_control as sbc
        sbc.set_brightness(level)
    return f"Brightness set to {level}%."


# ================================================================ keyboard / windows

def type_text(text: str):
    _type(text)
    return "Typed it."


def press_keys(keys: str):
    _hotkey(*keys.replace(" ", "").split("+"))
    return f"Pressed {keys}."


WIN_WINDOW = {"minimize_all": ("win", "d"), "show_desktop": ("win", "d"), "switch": ("alt", "tab"),
              "close_window": ("alt", "f4"), "maximize": ("win", "up"), "minimize": ("win", "down"),
              "snap_left": ("win", "left"), "snap_right": ("win", "right"), "task_view": ("win", "tab"),
              "screenshot_tool": ("win", "shift", "s")}
GNOME_WINDOW = {"switch": ("alt", "tab"), "close_window": ("alt", "f4"), "maximize": ("win", "up"),
                "minimize": ("win", "h"), "snap_left": ("win", "left"), "snap_right": ("win", "right"),
                "task_view": ("win",), "screenshot_tool": ("printscreen",), "show_desktop": ("win", "d"),
                "minimize_all": ("win", "d")}


def window_action(action: str):
    if IS_LINUX and action in ("minimize_all", "show_desktop") and not WAYLAND and _have("wmctrl"):
        _run(["wmctrl", "-k", "on"]); return "Showing desktop."
    keys = (GNOME_WINDOW if IS_LINUX else WIN_WINDOW).get(action)
    if not keys:
        return f"Unknown window action {action}."
    _hotkey(*keys)
    return f"Done: {action}."


# ================================================================ screen

def _grab_screen(max_w=1280):
    from PIL import Image
    img = None
    if not WAYLAND:
        try:
            import mss
            with mss.mss() as s:
                shot = s.grab(s.monitors[1])
                img = Image.frombytes("RGB", shot.size, shot.rgb)
        except Exception:
            img = None
    if img is None and IS_LINUX:
        tmp = Path("/tmp/jarvis_shot.png")
        for cmd in (["gnome-screenshot", "-f", str(tmp)], ["grim", str(tmp)], ["spectacle", "-b", "-n", "-o", str(tmp)]):
            if _have(cmd[0]):
                try:
                    _run(cmd); img = Image.open(tmp).convert("RGB"); break
                except Exception:
                    pass
    if img is None:
        raise RuntimeError("No screenshot backend (install gnome-screenshot, or use an X11 session)")
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    return img


def look_at_screen():
    buf = io.BytesIO()
    _grab_screen().save(buf, "JPEG", quality=70)
    return {"_image": buf.getvalue(), "result": "Screenshot of the user's screen has been sent to you. Describe/use it."}


def save_screenshot():
    folder = HOME / "Pictures" / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"jarvis_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
    _grab_screen(max_w=10_000).save(path)
    return f"Saved screenshot to {path}."


# ================================================================ system

def system_info():
    import psutil
    bat = psutil.sensors_battery()
    disk = psutil.disk_usage(str(HOME.anchor or "/"))
    return {"time": datetime.datetime.now().strftime("%A %d %B %Y, %I:%M %p"),
            "cpu_percent": psutil.cpu_percent(interval=0.5), "ram_percent": psutil.virtual_memory().percent,
            "disk_free_gb": round(disk.free / 1e9, 1),
            "battery": None if not bat else {"percent": round(bat.percent), "plugged_in": bat.power_plugged}}


def system_power(action: str, confirmed: bool = False):
    if action in ("shutdown", "restart", "sleep") and not confirmed:
        return f"NOT DONE. Ask the user to confirm {action} out loud, then call again with confirmed=true."
    if IS_LINUX:
        cmds = {"lock": ["loginctl", "lock-session"], "sleep": ["systemctl", "suspend"],
                "shutdown": ["shutdown", "-h", "+1"], "restart": ["shutdown", "-r", "+1"],
                "cancel_shutdown": ["shutdown", "-c"]}
        _run(cmds[action], check=False)
        extra = " in 1 minute (say 'cancel shutdown' to abort)" if action in ("shutdown", "restart") else ""
    else:
        cmds = {"lock": "rundll32.exe user32.dll,LockWorkStation", "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
                "shutdown": "shutdown /s /t 30", "restart": "shutdown /r /t 30", "cancel_shutdown": "shutdown /a"}
        subprocess.Popen(cmds[action], shell=True)
        extra = " in 30 seconds (say 'cancel shutdown' to abort)" if action in ("shutdown", "restart") else ""
    return f"{action} started{extra}."


FOLDERS = {k: HOME / v for k, v in {"downloads": "Downloads", "documents": "Documents", "desktop": "Desktop",
                                      "pictures": "Pictures", "music": "Music", "videos": "Videos", "home": "."}.items()}


def open_folder(name: str):
    p = FOLDERS.get(name.lower().strip(), Path(os.path.expandvars(os.path.expanduser(name))))
    if not p.exists():
        return f"Folder not found: {name}"
    _open_path(p)
    return f"Opened {p.resolve()}."


def find_files(query: str, folder: str = "home", open_first: bool = False):
    root = FOLDERS.get(folder.lower(), Path(os.path.expanduser(folder)))
    q, hits, scanned = query.lower(), [], 0
    skip = {"node_modules", "appdata", "__pycache__", "venv", "site-packages", "snap"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip and not d.startswith(".")]
        for f in filenames:
            scanned += 1
            if q in f.lower():
                hits.append(str(Path(dirpath) / f))
        if len(hits) >= 10 or scanned > 200_000:
            break
    hits = hits[:10]
    if hits and open_first:
        _open_path(hits[0])
    return {"matches": hits} if hits else f"No files matching '{query}' in {root}."


def clipboard(action: str, text: str = ""):
    import pyperclip
    if action == "set":
        pyperclip.copy(text); return "Copied to clipboard."
    return {"clipboard": pyperclip.paste()[:4000]}


def set_timer(seconds: int, label: str = "timer"):
    threading.Timer(seconds, lambda: announce(f"{config.USER_NAME}, your {label} is done.")).start()
    return f"Timer '{label}' set for {seconds} seconds."


def run_command(command: str, confirmed: bool = False):
    if not config.ALLOW_SHELL:
        return "Shell commands are disabled (set ALLOW_SHELL=true in .env)."
    if not confirmed:
        return "NOT RUN. Read the command back to the user, get a spoken yes, then call again with confirmed=true."
    cmd = ["powershell", "-NoProfile", "-Command", command] if IS_WIN else ["bash", "-lc", command]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return {"exit_code": r.returncode, "stdout": r.stdout[-3000:], "stderr": r.stderr[-1500:]}


# ================================================================ declarations

def _fn(name, desc, props=None, required=None):
    d = {"name": name, "description": desc}
    if props:
        d["parameters"] = {"type": "OBJECT", "properties": props, "required": required or []}
    return d


S, I, B = {"type": "STRING"}, {"type": "INTEGER"}, {"type": "BOOLEAN"}


def _enum(*vals):
    return {"type": "STRING", "enum": list(vals)}


OS_NAME = "Windows" if IS_WIN else ("Linux (GNOME, Wayland)" if WAYLAND else "Linux (X11)")

DECLARATIONS = [
    _fn("open_app", "Open/launch an application on the PC. For YouTube, open the website in the browser.", {"name": S}, ["name"]),
    _fn("close_app", "Close/quit a running application.", {"name": S}, ["name"]),
    _fn("open_website", "Open a URL in the default browser. For YouTube, use https://www.youtube.com.", {"url": S}, ["url"]),
    _fn("search_web", "Search the web in the browser.", {"query": S, "site": _enum("google", "youtube", "maps", "github", "stackoverflow")}, ["query"]),
    _fn("play_youtube", "Play a song/video on YouTube.", {"query": S}, ["query"]),
    _fn("volume", "Control system volume. value = percent for set, step for up/down.", {"action": _enum("set", "up", "down", "mute", "unmute", "get"), "value": I}, ["action"]),
    _fn("media_control", "Control media playback (Spotify, YouTube, VLC...).", {"action": _enum("play_pause", "next", "previous", "stop")}, ["action"]),
    _fn("set_brightness", "Set screen brightness 0-100.", {"level": I}, ["level"]),
    _fn("type_text", "Type text into the currently focused window.", {"text": S}, ["text"]),
    _fn("press_keys", "Press a keyboard shortcut, e.g. 'ctrl+s', 'alt+tab', 'enter'.", {"keys": S}, ["keys"]),
    _fn("window_action", "Manage windows.", {"action": _enum("minimize_all", "show_desktop", "switch", "close_window", "maximize", "minimize", "snap_left", "snap_right", "task_view", "screenshot_tool")}, ["action"]),
    _fn("look_at_screen", "Capture the screen so you can SEE it. Use for 'look at this', 'what's on my screen', 'read this error'."),
    _fn("save_screenshot", "Save a screenshot file to Pictures/Screenshots."),
    _fn("system_info", "Get time, battery, CPU, RAM and disk info."),
    _fn("system_power", "Lock, sleep, shutdown, restart, or cancel shutdown. shutdown/restart/sleep need confirmed=true after the user says yes.", {"action": _enum("lock", "sleep", "shutdown", "restart", "cancel_shutdown"), "confirmed": B}, ["action"]),
    _fn("open_folder", "Open a folder: downloads, documents, desktop, pictures, music, videos, home, or a path.", {"name": S}, ["name"]),
    _fn("find_files", "Find files by name.", {"query": S, "folder": S, "open_first": B}, ["query"]),
    _fn("clipboard", "Read or set the clipboard.", {"action": _enum("get", "set"), "text": S}, ["action"]),
    _fn("set_timer", "Set a countdown timer/reminder. Convert minutes to seconds.", {"seconds": I, "label": S}, ["seconds"]),
    _fn("run_command", f"Run a {'PowerShell' if IS_WIN else 'bash'} command. Only when nothing else fits. Needs confirmed=true after the user approves.", {"command": S, "confirmed": B}, ["command"]),
]

REGISTRY = {d["name"]: globals()[d["name"]] for d in DECLARATIONS}


def call(name: str, args: dict):
    fn = REGISTRY.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    try:
        _com_init()
        out = fn(**(args or {}))
        return out if isinstance(out, dict) else {"result": out}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
