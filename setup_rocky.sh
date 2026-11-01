#!/usr/bin/env bash
# One-time setup for Rocky Linux 9/10 (also RHEL/Alma/Fedora). Run as your normal user: ./setup_rocky.sh
set -e
cd "$(dirname "$0")"

echo "==> System packages (sudo)"
sudo dnf install -y epel-release || true
sudo dnf config-manager --set-enabled crb 2>/dev/null || sudo crb enable 2>/dev/null || true
# strict=0: skip any package your release doesn't have instead of failing
sudo dnf install -y --setopt=strict=0 \
  python3.12 python3.12-devel python3-devel gcc portaudio portaudio-devel \
  wireplumber pulseaudio-utils playerctl brightnessctl \
  wl-clipboard xclip xdotool wmctrl ydotool gnome-screenshot \
  chromium python3-tkinter

PY=$(command -v python3.12 || command -v python3)
echo "==> Python venv with $PY"
$PY -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# brightnessctl without sudo
sudo usermod -aG video "$USER" || true

# Wayland: keyboard/typing goes through ydotool (needs its daemon + /dev/uinput)
if command -v ydotoold >/dev/null; then
  echo "==> ydotoold service (keyboard control on Wayland)"
  sudo tee /etc/systemd/system/ydotoold.service >/dev/null <<EOF
[Unit]
Description=ydotool daemon
[Service]
ExecStart=$(command -v ydotoold) --socket-path=/tmp/.ydotool_socket --socket-own=$(id -u):$(id -g)
Restart=always
[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now ydotoold
else
  echo "!! ydotool not available. On Wayland, typing/shortcuts won't work."
  echo "   Fix: log out -> gear icon -> 'GNOME on Xorg' (Rocky 9), or build ydotool from source."
fi

# Local LLM brain (Ollama). Skip with: NO_OLLAMA=1 ./setup_rocky.sh
if [ -z "$NO_OLLAMA" ]; then
  if ! command -v ollama >/dev/null; then
    echo "==> Installing Ollama"
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  sudo systemctl enable --now ollama 2>/dev/null || true
  MODEL=$(grep -E '^OLLAMA_MODEL=' .env 2>/dev/null | cut -d= -f2 | cut -d' ' -f1)
  MODEL=${MODEL:-qwen3:4b}
  echo "==> Pulling $MODEL (one-time download)"
  ollama pull "$MODEL"
fi

[ -f .env ] || cp .env.example .env
echo
echo "Done. Next:"
echo "  1) Put your key in .env:   nano .env      (GEMINI_API_KEY=...)"
echo "  2) Start:                  ./run.sh"
echo "  (Log out/in once so the 'video' group applies for brightness.)"
