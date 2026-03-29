#!/bin/bash
set -e

echo "👀 Monitoring server for 5h 30m..."

RUNTIME=$((5 * 60 * 60 + 30 * 60))
INTERVAL=180

for ((i=0; i<RUNTIME; i+=INTERVAL)); do
  ELAPSED=$((i / 60))
  REMAINING=$(((RUNTIME - i) / 60))
  
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "⏰ Elapsed: ${ELAPSED}m | Remaining: ${REMAINING}m"
  
  # Check Minecraft
  if screen -list | grep -q "minecraft"; then
    echo "✅ Minecraft: Running"
    if [ -f minecraft-server/logs/latest.log ]; then
      tail -2 minecraft-server/logs/latest.log
    fi
  else
    echo "❌ Minecraft crashed! Restarting..."
    cd minecraft-server
    screen -dmS minecraft java -Xms6G -Xmx12G -jar paper.jar --nogui
    cd ..
    sleep 40
  fi
  
  # Check Playit
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
