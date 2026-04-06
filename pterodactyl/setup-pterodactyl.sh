#!/bin/bash
set -e

#############################################
# Pterodactyl Panel + Wings + NookTheme Setup
# For GitHub Actions ephemeral runner
#############################################

PANEL_URL="${APP_URL:-http://localhost}"
ADMIN_EMAIL="${PANEL_ADMIN_EMAIL:-admin@pterodactyl.io}"
ADMIN_USERNAME="${PANEL_ADMIN_USER:-admin}"
ADMIN_PASSWORD="${PANEL_PASSWORD:-admin123}"
FIRST_NAME="${PANEL_FIRST_NAME:-Admin}"
LAST_NAME="${PANEL_LAST_NAME:-User}"
WINGS_DIR="/etc/pterodactyl"
WINGS_DATA="/var/lib/pterodactyl/volumes"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[SETUP]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[SETUP]${NC} $1"; }
log_error() { echo -e "${RED}[SETUP]${NC} $1"; }

echo "============================================"
echo "  Pterodactyl + NookTheme Setup"
echo "============================================"

# =============================================
# Step 1: Build and start Docker containers
# =============================================
log_info "Building Pterodactyl Panel Docker image (this may take a few minutes)..."
cd /home/runner/work/papermcpe/papermcpe/pterodactyl

docker compose build --no-cache panel 2>&1 | tail -5

log_info "Starting Docker services..."
DB_ROOT_PASSWORD="$(openssl rand -hex 16)" \
DB_PASSWORD="$(openssl rand -hex 16)" \
HASHIDS_SALT="$(openssl rand -hex 16)" \
APP_URL="$PANEL_URL" \
docker compose up -d database cache panel

log_info "Waiting for Panel to be ready..."

# Wait for panel to be responsive
MAX_WAIT=180
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -sf -o /dev/null "$PANEL_URL" 2>/dev/null; then
        log_info "Panel is responding!"
        break
    fi
    sleep 5
    WAITED=$((WAITED + 5))
    echo "  Waiting... (${WAITED}s/${MAX_WAIT}s)"

    # Check if panel container is still running
    if ! docker compose ps panel | grep -q "running"; then
        log_error "Panel container stopped unexpectedly!"
        docker compose logs panel --tail 50
        exit 1
    fi
done

if [ $WAITED -ge $MAX_WAIT ]; then
    log_error "Panel did not respond within ${MAX_WAIT}s!"
    docker compose logs panel --tail 100
    log_warn "Attempting to continue anyway..."
fi

# =============================================
# Step 2: Create admin user
# =============================================
log_info "Creating admin user..."

docker compose exec -T panel php artisan p:user:make \
    --email="$ADMIN_EMAIL" \
    --username="$ADMIN_USERNAME" \
    --password="$ADMIN_PASSWORD" \
    --name-first="$FIRST_NAME" \
    --name-last="$LAST_NAME" \
    --admin=1 \
    --no-interaction 2>&1 || log_warn "Admin user may already exist (this is OK)"

log_info "Admin user created: $ADMIN_EMAIL / $ADMIN_PASSWORD"

# =============================================
# Step 3: Login to get session cookie & API key
# =============================================
log_info "Authenticating with Panel API..."

# Get CSRF cookie first
CSRF_COOKIE=$(curl -sf -c /tmp/ptero_cookies -b /tmp/ptero_cookies "$PANEL_URL/csrf-cookie" 2>/dev/null | grep -oP '(?<="csrf_token":")[^"]+' || echo "")

if [ -z "$CSRF_COOKIE" ]; then
    # Try alternate approach - get XSRF token from cookies
    CSRF_TOKEN=$(grep -oP 'XSRF-TOKEN=\K[^;]+' /tmp/ptero_cookies 2>/dev/null | python3 -c "import sys,urllib.parse;print(urllib.parse.unquote(sys.stdin.read().strip()))" 2>/dev/null || echo "")
else
    CSRF_TOKEN="$CSRF_COOKIE"
fi

# Login to get API token
LOGIN_RESPONSE=$(curl -sf -c /tmp/ptero_cookies -b /tmp/ptero_cookies \
    -H "Content-Type: application/json" \
    -H "X-XSRF-TOKEN: $CSRF_TOKEN" \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
    "$PANEL_URL/auth/login" 2>/dev/null || echo '{}')

# Extract the API key from login response
API_KEY=$(echo "$LOGIN_RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if 'attributes' in data:
        meta = data['attributes'].get('meta', {})
        # Get the api_key from the response or generate one
        key = data['attributes'].get('api_key', data['attributes'].get('token', ''))
        print(key)
    else:
        print('')
except:
    print('')
" 2>/dev/null || echo "")

if [ -z "$API_KEY" ]; then
    log_warn "Could not extract API key from login. Will create via CLI..."
    # Generate application API key directly via artisan
    API_KEY=$(docker compose exec -T panel php artisan p:application:create \
        --description="Wings Communication" \
        --no-interaction 2>&1 | grep -oP '(\w{8}-\w{4}-\w{4}-\w{4}-\w{12})' || echo "")

    if [ -z "$API_KEY" ]; then
        # Try to parse it differently
        API_KEY=$(docker compose exec -T panel php artisan p:application:create \
            --description="Wings Communication" \
            --no-interaction 2>&1 | tail -1 || echo "")
    fi
fi

log_info "Application API Key: ${API_KEY:0:20}..."

# =============================================
# Step 4: Create Location & Node via API
# =============================================
log_info "Setting up Node configuration..."

# Get fresh CSRF token
CSRF_COOKIE=$(curl -sf -c /tmp/ptero_cookies -b /tmp/ptero_cookies "$PANEL_URL/csrf-cookie" 2>/dev/null | grep -oP '(?<="csrf_token":")[^"]+' || echo "")
if [ -z "$CSRF_COOKIE" ]; then
    CSRF_TOKEN=$(grep -oP 'XSRF-TOKEN=\K[^;]+' /tmp/ptero_cookies 2>/dev/null | python3 -c "import sys,urllib.parse;print(urllib.parse.unquote(sys.stdin.read().strip()))" 2>/dev/null || echo "")
else
    CSRF_TOKEN="$CSRF_COOKIE"
fi

# Create Location (if not exists)
LOCATION_RESPONSE=$(curl -sf -b /tmp/ptero_cookies \
    -H "Content-Type: application/json" \
    -H "X-XSRF-TOKEN: $CSRF_TOKEN" \
    -H "Authorization: Bearer $API_KEY" \
    -d '{"short":"ghactions","long":"GitHub Actions Runner"}' \
    "$PANEL_URL/api/application/locations" 2>/dev/null || echo '{}')

LOCATION_ID=$(echo "$LOCATION_RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if 'attributes' in data:
        print(data['attributes']['id'])
    else:
        print('')
except:
    print('')
" 2>/dev/null || echo "")

if [ -z "$LOCATION_ID" ]; then
    log_warn "Location might already exist. Trying to find it..."
    LOCATIONS=$(curl -sf -b /tmp/ptero_cookies \
        -H "Authorization: Bearer $API_KEY" \
        "$PANEL_URL/api/application/locations" 2>/dev/null || echo '{"data":[]}')
    LOCATION_ID=$(echo "$LOCATIONS" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for item in data.get('data', []):
        if item['attributes']['short'] == 'ghactions':
            print(item['attributes']['id'])
            break
    else:
        print('')
except:
    print('')
" 2>/dev/null || echo "")
fi

log_info "Location ID: ${LOCATION_ID:-unknown}"

# =============================================
# Step 5: Setup Wings
# =============================================
log_info "Setting up Wings daemon..."

# Install Wings
mkdir -p "$WINGS_DIR" "$WINGS_DATA"

# Download latest Wings release
if [ ! -f /usr/local/bin/wings ]; then
    log_info "Downloading Wings binary..."
    curl -sfL "https://github.com/pterodactyl/wings/releases/latest/download/wings_linux_amd64" -o /usr/local/bin/wings
    chmod +x /usr/local/bin/wings
    log_info "Wings installed to /usr/local/bin/wings"
else
    log_info "Wings already installed"
fi

# Create Wings configuration
NODE_TOKEN="$(openssl rand -hex 32)"

# Get the container IP for internal communication
PANEL_INTERNAL_IP=$(docker network inspect pterodactyl_pterodactyl_net 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for net in data[0].get('Containers', {}).values():
        if 'panel' in net.get('Name', ''):
            print(net.get('IPv4Address', '').split('/')[0])
            break
    else:
        print('panel')
except:
    print('panel')
" 2>/dev/null || echo "panel")

log_info "Panel internal address: ${PANEL_INTERNAL_IP}"

cat > "$WINGS_DIR/config.yml" << WINGS_CONFIG
debug: false

api:
  host: 0.0.0.0
  port: 8080
  ssl:
    enabled: false
    cert: /etc/letsencrypt/live/pterodactyl/fullchain.pem
    key: /etc/letsencrypt/live/pterodactyl/privkey.pem

remote: "http://${PANEL_INTERNAL_IP}"

token: "${NODE_TOKEN}"

filesystem:
  # Docker volumes will be used for server data
  servers: ${WINGS_DATA}

docker:
  # Use the host Docker daemon
  socket: /var/run/docker.sock
  # Network for game server containers
  network: pterodactyl_network

sftp:
  host: 0.0.0.0
  port: 2022
  key_size: 2048

panel:
  location_id: ${LOCATION_ID:-1}

limits:
  memory: 0
  swap: -1
  disk: 0
  io: 500
  cpu: 0

allocations:
  default:
    - "0.0.0.0/0"
WINGS_CONFIG

log_info "Wings configuration created"

# If we have a valid API key and location, try to register the node with the panel
if [ -n "$API_KEY" ] && [ -n "$LOCATION_ID" ]; then
    log_info "Registering node with Panel..."

    # Get the machine's IP
    MACHINE_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")

    NODE_CREATE_RESPONSE=$(curl -sf -b /tmp/ptero_cookies \
        -H "Content-Type: application/json" \
        -H "X-XSRF-TOKEN: $CSRF_TOKEN" \
        -H "Authorization: Bearer $API_KEY" \
        -d "{
            \"name\": \"GitHub Actions Node\",
            \"description\": \"Auto-provisioned node on GitHub Actions runner\",
            \"location_id\": $LOCATION_ID,
            \"public\": true,
            \"fqdn\": \"${MACHINE_IP}\",
            \"scheme\": \"http\",
            \"behind_proxy\": false,
            \"memory\": $(free -m 2>/dev/null | awk '/Mem:/{print $2}' || echo 7000),
            \"memory_overallocate\": 0,
            \"disk\": $(df -m / 2>/dev/null | awk 'NR==2{print $4}' || echo 30000),
            \"disk_overallocate\": 0,
            \"upload_size\": 200,
            \"daemon_sftp\": 2022,
            \"daemon_listen\": 8080
        }" \
        "$PANEL_URL/api/application/nodes" 2>/dev/null || echo '{}')

    NODE_ID=$(echo "$NODE_CREATE_RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if 'attributes' in data:
        print(data['attributes']['id'])
    else:
        print('')
except:
    print('')
" 2>/dev/null || echo "")

    if [ -n "$NODE_ID" ]; then
        log_info "Node registered with ID: $NODE_ID"

        # Create allocations for this node
        for PORT in 25565 19132 25575; do
            ALLOC_RESPONSE=$(curl -sf -b /tmp/ptero_cookies \
                -H "Content-Type: application/json" \
                -H "X-XSRF-TOKEN: $CSRF_TOKEN" \
                -H "Authorization: Bearer $API_KEY" \
                -d "{\"allocation_default\":true,\"alias\":\"Minecraft Port $PORT\",\"ports\":{\"$PORT\":{\"ip\":\"0.0.0.0\",\"ports\":\"$PORT\"}}}" \
                "$PANEL_URL/api/application/nodes/$NODE_ID/allocations" 2>/dev/null || echo '{}')
            log_info "Allocation for port $PORT: $(echo "$ALLOC_RESPONSE" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("attributes",{}).get("id","failed"))' 2>/dev/null || echo 'created')"
        done

        # Get the proper Wings configuration from the panel
        log_info "Generating proper Wings configuration from Panel..."
        WINGS_YAML=$(docker compose exec -T panel php artisan p:node:configuration "$NODE_ID" 2>/dev/null || echo "")

        if [ -n "$WINGS_YAML" ]; then
            echo "$WINGS_YAML" > "$WINGS_DIR/config.yml"
            log_info "Wings configuration updated from Panel!"
        fi
    else
        log_warn "Could not register node via API. Using local configuration."
        log_warn "You may need to create the node manually in the Panel admin area."
    fi
else
    log_warn "Skipping node registration (no API key or location ID)"
    log_warn "You'll need to create the node manually in the Panel admin area."
fi

# Create Docker network for game servers
docker network create pterodactyl_network 2>/dev/null || true

# =============================================
# Step 6: Start Wings
# =============================================
log_info "Starting Wings daemon..."
wings > /var/log/wings.log 2>&1 &
WINGS_PID=$!
echo "$WINGS_PID" > /tmp/wings.pid

sleep 3

if kill -0 $WINGS_PID 2>/dev/null; then
    log_info "Wings is running (PID: $WINGS_PID)"
else
    log_error "Wings failed to start! Check /var/log/wings.log"
    tail -50 /var/log/wings.log 2>/dev/null || echo "No logs found"
    log_warn "Continuing without Wings. You can start it manually."
fi

# =============================================
# Summary
# =============================================
echo ""
echo "============================================"
echo "  Setup Complete!"
echo "============================================"
echo ""
echo "  Panel URL:     $PANEL_URL"
echo "  Admin Email:   $ADMIN_EMAIL"
echo "  Admin User:    $ADMIN_USERNAME"
echo "  Admin Pass:    $ADMIN_PASSWORD"
echo "  Wings Status:  $(kill -0 $WINGS_PID 2>/dev/null && echo 'Running' || echo 'Stopped')"
echo ""
echo "  Docker containers:"
docker compose ps
echo ""
echo "============================================"
