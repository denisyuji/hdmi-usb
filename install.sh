#!/bin/bash

echo "[INFO] Installing hdmi-usb..."
mkdir -p ~/.local/bin
cp ./hdmi-usb.py ~/.local/bin/hdmi-usb.py
cp ./hdmi-usb ~/.local/bin/hdmi-usb
cp ./screenshot-hdmi-usb ~/.local/bin/screenshot-hdmi-usb

# Ensure scripts are executable
chmod +x ~/.local/bin/hdmi-usb ~/.local/bin/hdmi-usb.py ~/.local/bin/screenshot-hdmi-usb

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
    echo "# Added by AppImage installer on $(date)"
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

echo "[INFO] hdmi-usb installed successfully!"
echo "[INFO] You can now use hdmi-usb by running 'hdmi-usb' in your terminal."
echo "[INFO] Screenshot tool: 'screenshot-hdmi-usb'"