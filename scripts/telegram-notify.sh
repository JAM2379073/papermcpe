#!/bin/bash

send_telegram() {
  curl -s -X POST https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage \
    -d chat_id=${TELEGRAM_CHAT_ID} \
    -d parse_mode=HTML \
    -d text="$1" > /dev/null
}

case "$1" in
  starting)
    send_telegram "🎮 <b>Minecraft Paper Server Starting!</b>%0A%0A⏰ $(date '+%Y-%m-%d %H:%M:%S')%0A🔄 Run #${RUN_NUMBER}%0A📦 Downloading backup..."
    ;;
    
  online)
    send_telegram "✅ <b>Minecraft Server ONLINE!</b>%0A%0A🌐 <b>Connection:</b> Check Playit.gg dashboard%0A📱 <b>Supports:</b> Java + Bedrock%0A⏰ <b>Started:</b> $(date '+%H:%M:%S')%0A⏳ <b>Runtime:</b> 5h 30m%0A🔄 <b>Run:</b> #${RUN_NUMBER}%0A%0A🎯 <b>Plugins:</b>%0A• ViaVersion + ViaBackwards%0A• Geyser + Floodgate%0A%0A🎮 Server is ready!"
    ;;
    
  backup-done)
    send_telegram "💾 <b>Backup Completed!</b>%0A%0A📦 <b>Size:</b> ${BACKUP_SIZE}%0A☁️ <b>Storage:</b> HuggingFace%0A⏰ <b>Time:</b> $(date '+%H:%M:%S')%0A%0A🔄 Starting new instance..."
    ;;
    
  restarting)
    send_telegram "🔄 <b>New Server Instance Starting!</b>%0A%0A⏳ Server will be online in ~2-3 minutes%0A✅ All progress saved%0A🌐 Same Playit.gg address%0A%0A🎮 Get ready to reconnect!"
    ;;
    
  *)
    echo "Usage: $0 {starting|online|backup-done|restarting}"
    exit 1
    ;;
esac

echo "✅ Telegram notification sent: $1"
