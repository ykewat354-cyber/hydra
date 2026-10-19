#!/data/data/com.termux/files/usr/bin/sh
# HYDRA installer for Termux. No pip. Writes $PREFIX/bin/hydra.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
LAUNCHER="$PREFIX/bin/hydra"

cat > "$LAUNCHER" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
PYTHONPATH="$DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python -m hydra "\$@"
EOF
chmod +x "$LAUNCHER"

echo "hydra installed -> $LAUNCHER"
echo "repo: $DIR"
echo
echo "next:"
echo "  hydra                 # open the full-screen TUI"
echo "  hydra doctor          # environment checks"
echo "  hydra models probe    # discover which models your key can use"
echo "  hydra \"your task\"     # autonomous run"
