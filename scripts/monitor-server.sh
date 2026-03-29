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
    
    # Use Java 21 explicitly for restart
    echo "🔄 Restarting with Java 21..."
    /usr/lib/jvm/java-21-openjdk-amd64/bin/java -Xmx3500M -Xms2G \
      -XX:+UseG1GC \
      -XX:+ParallelRefProcEnabled \
      -XX:MaxGCPauseMillis=200 \
      -XX:+UnlockExperimentalVMOptions \
      -XX:+DisableExplicitGC \
      -XX:+AlwaysPreTouch \
      -XX:G1NewSizePercent=30 \
      -XX:G1MaxNewSizePercent=40 \
      -XX:G1HeapRegionSize=8M \
      -XX:G1ReservePercent=20 \
      -XX:G1HeapWastePercent=5 \
      -XX:G1MixedGCCountTarget=4 \
      -XX:InitiatingHeapOccupancyPercent=15 \
      -XX:G1MixedGCLiveThresholdPercent=90 \
      -XX:G1RSetUpdatingPauseTimePercent=5 \
      -XX:SurvivorRatio=32 \
      -XX:+PerfDisableSharedMem \
      -XX:MaxTenuringThreshold=1 \
      -jar paper.jar --nogui &
    
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
