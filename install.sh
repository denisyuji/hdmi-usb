#!/bin/bash

echo "[INFO] Installing hdmi-usb..."
mkdir -p ~/.local/bin
cp ./hdmi-usb.py ~/.local/bin/hdmi-usb.py
cp ./hdmi-usb ~/.local/bin/hdmi-usb
cp ./hdmi-usb-screenshot-mcp ~/.local/bin/hdmi-usb-screenshot-mcp

# Ensure scripts are executable
chmod +x ~/.local/bin/hdmi-usb ~/.local/bin/hdmi-usb.py ~/.local/bin/hdmi-usb-screenshot-mcp

# === Ensure ~/.local/bin is in PATH ===
if ! echo ":$PATH:" | grep -q ":$HOME/.local/bin:"; then
  SHELL_NAME="$(basename "$SHELL")"
  case "$SHELL_NAME" in
    bash)
      SHELL_RC="$HOME/.bashrc"
      ;;
    zsh)
      SHELL_RC="$HOME/.zshrc"
      ;;
    fish)
      SHELL_RC="$HOME/.config/fish/config.fish"
      ;;
    *)
      SHELL_RC="$HOME/.profile"
      ;;
  esac

  echo "[INFO] Adding ~/.local/bin to PATH in $SHELL_RC"
  {
    echo ""
    echo "# Added by hdmi-usb installer on $(date)"
    if [[ "$SHELL_NAME" == "fish" ]]; then
      echo "set -U fish_user_paths \$HOME/.local/bin \$fish_user_paths"
    else
      echo 'export PATH="$HOME/.local/bin:$PATH"'
    fi
  } >> "$SHELL_RC"

  echo "[INFO] ~/.local/bin added to PATH. Restart your shell or run:"
  if [[ "$SHELL_NAME" == "fish" ]]; then
    echo "       set -U fish_user_paths \$HOME/.local/bin \$fish_user_paths"
  else
    echo "       export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
else
  echo "[INFO] ~/.local/bin already in PATH"
fi

# === Cursor MCP: ~/.cursor/mcp.json ===
python3 - <<'PY'
import json
import sys
from pathlib import Path

mcp_path = Path.home() / ".cursor" / "mcp.json"
cmd = Path.home() / ".local" / "bin" / "hdmi-usb-screenshot-mcp"

mcp_path.parent.mkdir(parents=True, exist_ok=True)

if mcp_path.exists():
    try:
        raw = mcp_path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"[WARN] Skip MCP config: invalid JSON in {mcp_path}: {e}", file=sys.stderr)
        sys.exit(0)
else:
    data = {}

if not isinstance(data, dict):
    data = {}
servers = data.get("mcpServers")
if not isinstance(servers, dict):
    servers = {}
data["mcpServers"] = servers

servers["hdmi-screenshot"] = {
    "command": str(cmd),
    "env": {
        "RTSP_URL": "rtsp://127.0.0.1:1234/hdmi",
        "PYTHONUNBUFFERED": "1",
    },
}

mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(f"[INFO] MCP server 'hdmi-screenshot' -> {mcp_path}")
PY

echo "[INFO] hdmi-usb installed successfully!"
echo "[INFO] You can now use hdmi-usb by running 'hdmi-usb' in your terminal."
echo "[INFO] MCP screenshot server: 'hdmi-usb-screenshot-mcp'"