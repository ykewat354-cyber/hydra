#!/data/data/com.termux/files/usr/bin/sh
# One-line install:  curl -fsSL <raw-url>/install-remote.sh | bash
# Clones (or updates) HYDRA into ~/hydra, installs the launcher, runs doctor.
set -e

REPO="${HYDRA_REPO:-https://github.com/ykewat354-cyber/hydra}"
DIR="$HOME/hydra"

have() { command -v "$1" >/dev/null 2>&1; }

if have pkg; then
  have python || pkg install -y python
  have git    || pkg install -y git
  have curl   || pkg install -y curl
  have rg     || pkg install -y ripgrep || true
fi

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only || true
else
  git clone --depth 1 "$REPO" "$DIR"
fi

sh "$DIR/install.sh"
"${PREFIX:-/data/data/com.termux/files/usr}/bin/hydra" doctor || true
