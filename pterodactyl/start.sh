#!/bin/sh
set -e

cd /var/www/pterodactyl

echo "[Pterodactyl] Starting initialization..."

# Create .env from environment variables
cat > .env << INI_EOF
APP_ENV=${APP_ENV:-production}
APP_DEBUG=${APP_DEBUG:-false}
APP_KEY=
APP_THEME=pterodactyl
APP_TIMEZONE=${APP_TIMEZONE:-UTC}
APP_URL=${APP_URL:-http://localhost}
APP_LOCALE=en
APP_ENVIRONMENT_ONLY=false

LOG_CHANNEL=daily
LOG_LEVEL=warning

DB_CONNECTION=mysql
DB_HOST=${DB_HOST:-database}
DB_PORT=${DB_PORT:-3306}
DB_DATABASE=${DB_DATABASE:-panel}
DB_USERNAME=${DB_USERNAME:-pterodactyl}
DB_PASSWORD=${DB_PASSWORD:-changeme}

CACHE_DRIVER=${CACHE_DRIVER:-redis}
SESSION_DRIVER=${SESSION_DRIVER:-redis}
QUEUE_CONNECTION=${QUEUE_CONNECTION:-redis}

REDIS_HOST=${REDIS_HOST:-cache}
REDIS_PASSWORD=${REDIS_PASSWORD:-null}
REDIS_PORT=${REDIS_PORT:-6379}

HASHIDS_SALT=${HASHIDS_SALT:-nookure_s3cr3t_salt}
HASHIDS_LENGTH=8

MAIL_MAILER=smtp
MAIL_HOST=${MAIL_HOST:-127.0.0.1}
MAIL_PORT=${MAIL_PORT:-1025}
MAIL_USERNAME=
MAIL_PASSWORD=
MAIL_ENCRYPTION=
MAIL_FROM_ADDRESS=${MAIL_FROM_ADDRESS:-noreply@pterodactyl.io}
MAIL_FROM_NAME="${MAIL_FROM_NAME:-Pterodactyl Panel}"

TRUSTED_PROXIES=*
INI_EOF

# Generate app key if not already set
APP_KEY_CURRENT=$(php artisan key:generate --force --no-interaction --show 2>/dev/null || echo "")
if [ -n "$APP_KEY_CURRENT" ]; then
    sed -i "s/^APP_KEY=$/APP_KEY=${APP_KEY_CURRENT}/" .env
fi

# Wait for database to be ready
echo "[Pterodactyl] Waiting for database..."
MAX_RETRIES=30
RETRY_COUNT=0
until php artisan migrate:status > /dev/null 2>&1 || [ $RETRY_COUNT -eq $MAX_RETRIES ]; do
    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo "[Pterodactyl] Database not ready, retrying... ($RETRY_COUNT/$MAX_RETRIES)"
    sleep 3
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "[Pterodactyl] ERROR: Database connection failed after $MAX_RETRIES attempts!"
    # Still start nginx/php-fpm so we can see errors in the web UI
    php-fpm -D 2>/dev/null || true
    nginx -g 'daemon off;'
    exit 1
fi

# Run migrations and seed
echo "[Pterodactyl] Running database migrations..."
php artisan migrate --force --no-interaction 2>/dev/null || echo "[Pterodactyl] Migrations may have already run"

echo "[Pterodactyl] Seeding database..."
php artisan db:seed --force --no-interaction 2>/dev/null || echo "[Pterodactyl] Seeding may have already run"

# Cache configuration for performance
echo "[Pterodactyl] Caching configuration..."
php artisan config:cache --no-interaction 2>/dev/null || true
php artisan route:cache --no-interaction 2>/dev/null || true
php artisan view:cache --no-interaction 2>/dev/null || true

# Create necessary directories with proper permissions
mkdir -p storage/framework/{sessions,views,cache}
chown -R www-data:www-data storage bootstrap/cache

echo "[Pterodactyl] Panel initialization complete!"

# Start queue worker in background
echo "[Pterodactyl] Starting queue worker..."
php artisan queue:work --queue=high,standard,low --sleep=3 --tries=3 --max-time=3600 > /dev/stdout 2>&1 &
QUEUE_PID=$!
echo $QUEUE_PID > /tmp/queue-worker.pid

# Start PHP-FPM in background
echo "[Pterodactyl] Starting PHP-FPM..."
php-fpm -D

# Give PHP-FPM a moment to start
sleep 2

# Start Nginx in foreground (keeps container running)
echo "[Pterodactyl] Starting Nginx..."
nginx -g 'daemon off;'
