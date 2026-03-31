#!/bin/bash
set -e

echo "🖥️ Setting up Panel + Cloudflared..."

# Install cloudflared
echo "☁️ Installing cloudflared..."
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb
rm cloudflared-linux-amd64.deb
echo "✅ Cloudflared installed"

# Install cloudflared service
echo "🔑 Installing cloudflared service..."
sudo cloudflared service install eyJhIjoiY2U5MWFiYmQyYWU3YjhlZjQxMmU0YWQxODJmYzUxYzciLCJ0IjoiZGRkMDg1NDMtOWI2Yy00OWI5LTg5YjItZjYwMjBiYjMwZjFhIiwicyI6Ik1tTTVOR1U0WkRNdE5HTm1ZUzAwWkRFM0xUZzRNamN0TkRJeE5tRTNOalE1TUdNNCJ9

# Start cloudflared
echo "🚀 Starting cloudflared..."
sudo systemctl start cloudflared
sudo systemctl status cloudflared --no-pager || true
echo "✅ Cloudflared started"

# Export environment variables for panel
export PANEL_PASSWORD="${PANEL_PASSWORD:-admin123}"
export PANEL_PORT="${PANEL_PORT:-8080}"

# Start panel
echo "🌐 Starting panel on port ${PANEL_PORT}..."
cd scripts/panel
python3 panel-server.py &
PANEL_PID=$!
echo $PANEL_PID > /tmp/panel.pid
cd ../..

sleep 3

if kill -0 $PANEL_PID 2>/dev/null; then
  echo "✅ Panel running on port ${PANEL_PORT} (PID: $PANEL_PID)"
  echo "✅ Default login: admin / admin123"
else
  echo "❌ Panel failed to start!"
  exit 1
fi
