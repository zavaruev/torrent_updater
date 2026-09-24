#!/bin/bash
set -e

# Clean up any stale Xvfb lock
rm -f /tmp/.X99-lock

# Start Xvfb in background for Selenium
Xvfb :99 -screen 0 1920x1080x24 &
export DISPLAY=:99

# Wait for Xvfb to start
sleep 2

# Run the main command
exec "$@"
