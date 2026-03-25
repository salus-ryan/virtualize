# Virtualize auto-start (one-shot — deletes itself after running)
if [ -t 0 ] && [ -t 1 ]; then
    VDIR="/home/owner/virtualize"
    rm -f "$VDIR/.virtualize_autostart.sh"
    sed -i '/\.virtualize_autostart\.sh/d' ~/.bashrc 2>/dev/null || true
    cd "$VDIR" && source .venv/bin/activate && exec virtualize
fi
