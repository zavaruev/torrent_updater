#!/bin/bash
set -e

# Clean up any stale Xvfb lock
rm -f /tmp/.X99-lock

# Start Xvfb in background for Selenium
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +extension RANDR +extension RENDER &
XVFB_PID=$!
export DISPLAY=:99

# Wait for Xvfb to start and verify
for i in {1..20}; do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo "Xvfb started successfully on :99 (PID: $XVFB_PID)"
        break
    fi
    echo "Waiting for Xvfb... ($i/20)"
    sleep 1
done

# Final verification
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
    echo "ERROR: Xvfb failed to start"
    exit 1
fi

# Verify Xvfb process is still alive
if ! kill -0 $XVFB_PID 2>/dev/null; then
    echo "ERROR: Xvfb process died"
    exit 1
fi

echo "Xvfb ready. Starting application..."
exec "$@"
