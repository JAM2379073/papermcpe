#!/bin/bash
set -e

echo "============================================"
echo "  Pterodactyl Panel + NookTheme Setup"
echo "  with Cloudflare Tunnel"
echo "============================================"

# =============================================
# Step 1: Install Cloudflared
# =============================================
echo "☁️  Installing Cloudflared..."
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb
rm cloudflared-linux-amd64.deb
echo "✅ Cloudflared installed"

# =============================================
# Step 2: Install Cloudflared tunnel service
# =============================================
echo "🔑 Configuring Cloudflare Tunnel..."
sudo cloudflared service install eyJhIjoiY2U5MWFiYmQyYWU3YjhlZjQxMmU0YWQxODJmYzUxYzciLCJ0IjoiZGRkMDg1NDMtOWI2Yy00OWI5LTg5YjItZjYwMjBiYjMwZjFhIiwicyI6Ik1tTTVOR1U0WkRNdE5HTm1ZUzAwWkRFM0xUZzRNamN0TkRJeE5tRTNOalE1TUdNNCJ9

echo "🚀 Starting Cloudflared..."
sudo systemctl start cloudflared
sleep 3
sudo systemctl status cloudflared --no-pager || true
echo "✅ Cloudflared started"

# =============================================
# Step 3: Install Docker Compose (if needed)
# =============================================
echo "🐳 Checking Docker..."
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed!"
    exit 1
fi

docker --version
docker compose version

# Ensure Docker daemon is running
sudo systemctl start docker 2>/dev/null || true
docker info > /dev/null 2>&1 || { echo "❌ Docker daemon not accessible!"; exit 1; }
echo "✅ Docker is ready"

# =============================================
# Step 4: Build and start Pterodactyl
# =============================================
echo "🏗️  Building Pterodactyl Panel with NookTheme..."

cd /home/runner/work/papermcpe/papermcpe/pterodactyl

# Generate random passwords for database
export DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-$(openssl rand -hex 16)}"
export DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 16)}"
export HASHIDS_SALT="${HASHIDS_SALT:-$(openssl rand -hex 16)}"
export APP_URL="${APP_URL:-https://panel.projectxglory.qzz.io}"

echo "  APP_URL: $APP_URL"
echo "  Building Docker image (this takes 2-5 minutes)..."

docker compose build --no-cache panel 2>&1 | while IFS= read -r line; do
    # Only show important lines
    if echo "$line" | grep -qiE "(step|error|warning|finish|success|done|copy|run)" 2>/dev/null; then
        echo "  $line"
    fi
done

echo "✅ Docker image built"

echo "🚀 Starting Pterodactyl services..."
docker compose up -d database cache panel

# Wait for Panel to be ready
echo "⏳ Waiting for Panel to initialize..."
MAX_WAIT=300
WAITED=0
PANEL_URL="$APP_URL"

while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -sf -o /dev/null "$PANEL_URL" 2>/dev/null; then
        echo "✅ Panel is responding at $PANEL_URL!"
        break
    fi

    # Also check if locally accessible
    if curl -sf -o /dev/null "http://localhost" 2>/dev/null; then
        echo "✅ Panel is responding locally!"
        PANEL_URL="http://localhost"
        break
    fi

    sleep 5
    WAITED=$((WAITED + 5))
    if [ $((WAITED % 30)) -eq 0 ]; then
        echo "  Still waiting... (${WAITED}s/${MAX_WAIT}s)"
        docker compose ps
    fi

    if ! docker compose ps panel 2>/dev/null | grep -q "running"; then
        echo "❌ Panel container stopped unexpectedly!"
        docker compose logs panel --tail 30
        exit 1
    fi
done

if [ $WAITED -ge $MAX_WAIT ]; then
    echo "⚠️  Panel did not respond within ${MAX_WAIT}s, but containers are running"
    docker compose ps
fi

# =============================================
# Step 5: Create admin user
# =============================================
echo "👤 Creating admin user..."

ADMIN_EMAIL="${PANEL_ADMIN_EMAIL:-admin@pterodactyl.io}"
ADMIN_USER="${PANEL_ADMIN_USER:-admin}"
ADMIN_PASS="${PANEL_PASSWORD:-admin123}"

docker compose exec -T panel php artisan p:user:make \
    --email="$ADMIN_EMAIL" \
    --username="$ADMIN_USER" \
    --password="$ADMIN_PASS" \
    --name-first="Admin" \
    --name-last="User" \
    --admin=1 \
    --no-interaction 2>&1 || echo "  (Admin user may already exist)"

echo "✅ Admin user configured"
echo ""
echo "============================================"
echo "  PTERODACTYL PANEL IS LIVE!"
echo "============================================"
echo ""
echo "  🌐 Panel URL: $APP_URL"
echo "  👤 Admin: $ADMIN_EMAIL"
echo "  🔑 Password: $ADMIN_PASS"
echo ""
echo "  📦 Running containers:"
docker compose ps
echo ""
echo "============================================"
