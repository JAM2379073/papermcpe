#!/bin/bash
set -e

echo "🛑 Starting graceful shutdown sequence..."

# Send Telegram warning
curl -s -X POST https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage \
  -d chat_id=${TELEGRAM_CHAT_ID} \
  -d parse_mode=HTML \
  -d text="⚠️ <b>Server Restarting in 10 Minutes!</b>%0A%0A💾 Save your items!%0A🏠 Go to a safe place!%0A⏰ $(date '+%H:%M:%S')" > /dev/null

# 10 minute warning
screen -S minecraft -X stuff "say §c§l━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(printf '\r')"
screen -S minecraft -X stuff "say §c§l[⚠] SERVER RESTARTING IN 10 MINUTES!$(printf '\r')"
screen -S minecraft -X stuff "say §e§l[!] Save your items to chests!$(printf '\r')"
screen -S minecraft -X stuff "say §c§l━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(printf '\r')"
sleep 120

# 8 minute warning
screen -S minecraft -X stuff "say §e[⚠] Restarting in 8 minutes!$(printf '\r')"
sleep 120

# 6 minute warning
screen -S minecraft -X stuff "say §e[⚠] Restarting in 6 minutes!$(printf '\r')"
sleep 120

# 4 minute warning
screen -S minecraft -X stuff "say §6[⚠] Restarting in 4 minutes!$(printf '\r')"
sleep 120

# 2 minute warning
screen -S minecraft -X stuff "say §6§l[⚠] Restarting in 2 minutes!$(printf '\r')"
sleep 60

# 1 minute warning
screen -S minecraft -X stuff "say §c§l[⚠] RESTARTING IN 1 MINUTE!$(printf '\r')"
sleep 30

# 30 second warning
screen -S minecraft -X stuff "say §c§l[⚠] RESTARTING IN 30 SECONDS!$(printf '\r')"
sleep 15

# 15 second warning
screen -S minecraft -X stuff "say §c§l[⚠] RESTARTING IN 15 SECONDS!$(printf '\r')"
sleep 10

# Final 5 second countdown
echo "⏰ Final 5 second countdown..."
screen -S minecraft -X stuff "say §4§l━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(printf '\r')"
screen -S minecraft -X stuff "say §4§l[!!!] RESTARTING IN 5 SECONDS [!!!]$(printf '\r')"
screen -S minecraft -X stuff "say §4§l━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(printf '\r')"
sleep 1

screen -S minecraft -X stuff "title @a times 10 40 10$(printf '\r')"
screen -S minecraft -X stuff "title @a title {\"text\":\"4\",\"color\":\"red\",\"bold\":true}$(printf '\r')"
screen -S minecraft -X stuff "say §4§l4...$(printf '\r')"
sleep 1

screen -S minecraft -X stuff "title @a title {\"text\":\"3\",\"color\":\"red\",\"bold\":true}$(printf '\r')"
screen -S minecraft -X stuff "say §4§l3...$(printf '\r')"
sleep 1

screen -S minecraft -X stuff "title @a title {\"text\":\"2\",\"color\":\"gold\",\"bold\":true}$(printf '\r')"
screen -S minecraft -X stuff "say §6§l2...$(printf '\r')"
sleep 1

screen -S minecraft -X stuff "title @a title {\"text\":\"1\",\"color\":\"yellow\",\"bold\":true}$(printf '\r')"
screen -S minecraft -X stuff "say §e§l1...$(printf '\r')"
sleep 1

screen -S minecraft -X stuff "title @a title {\"text\":\"SAVING...\",\"color\":\"green\",\"bold\":true}$(printf '\r')"
screen -S minecraft -X stuff "say §a§l💾 SAVING WORLD - PLEASE WAIT...$(printf '\r')"

# Save world
echo "💾 Saving world..."
screen -S minecraft -X stuff "save-all flush$(printf '\r')"
sleep 20

# Stop server
echo "🛑 Stopping server..."
screen -S minecraft -X stuff "stop$(printf '\r')"
sleep 30

# Force kill if needed
screen -S minecraft -X quit 2>/dev/null || true
pkill -9 java 2>/dev/null || true

# Stop Playit tunnel
echo "🛑 Stopping Playit tunnel..."
docker stop playit 2>/dev/null || true
docker rm playit 2>/dev/null || true

echo "✅ Server shutdown complete!"
