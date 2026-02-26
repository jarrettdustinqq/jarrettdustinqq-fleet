#!/usr/bin/env bash
set -euo pipefail

tmp_script="$(mktemp /tmp/nix-install.XXXXXX.sh)"
trap 'rm -f "$tmp_script"' EXIT

echo "[fleet] Downloading Nix installer..."
curl -fL --retry 8 --retry-delay 3 --retry-all-errors \
  https://nixos.org/nix/install -o "$tmp_script"

echo "[fleet] Running Nix installer (--no-daemon)..."
sh "$tmp_script" --no-daemon

echo "[fleet] Nix install finished."
echo "[fleet] Run: . \"$HOME/.nix-profile/etc/profile.d/nix.sh\" && nix --version"
