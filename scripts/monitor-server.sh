#!/bin/bash
set -e

echo "👀 Monitoring server for 5h 30m..."

RUNTIME=$((5 * 60 * 60 + 30 * 60))  # 5h 30m in seconds
INTERVAL=180  # Check every 3 minutes

for ((i=0; i<RUNTIME; i+=INTERVAL)); do
  ELAPSED=$((i / 60))
  REMAINING=$(((RUNTIME - i) / 60))
  
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "⏰ Elapsed: ${ELAPSED}m | Remaining: ${REMAINING}m"
  
  # Check if Minecraft server is running
  if screen -list | grep -q "minecraft"; then
    echo "✅ Minecraft: Running"
    
    # Show recent logs
    if [ -f minecraft-server/logs/latest.log ]; then
      echo "📝 Recent activity:"
      tail -3 minecraft-server/logs/latest.log
    fi
  else
    echo "❌ Minecraft crashed! Restarting..."
    cd minecraft-server
    screen -dmS minecraft java -Xmx3500M -Xms2G -jar paper.jar --nogui
    cd ..
    sleep 40
  fi
  
  # Check if Playit tunnel is running
  if docker ps | grep -q "playit"; then
    echo "✅ Playit: Running"
  else
    echo "⚠️ Playit stopped! Restarting..."
    docker run -d --name playit --net=host \
      -e SECRET_KEY=${PLAYIT_SECRET_KEY} \
      ghcr.io/playit-cloud/playit-agent:0.17
  fi
  
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  
  sleep $INTERVAL
done

echo "⏰ Monitoring complete. Starting shutdown..."
