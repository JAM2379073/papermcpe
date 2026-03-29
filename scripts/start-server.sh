#!/bin/bash
set -e

echo "🎮 Starting Minecraft server..."

cd minecraft-server

# Verify Java 21 is being used
echo "☕ Java version check:"
java -version 2>&1 | head -n 1

# Check if Java 21 is actually being used
JAVA_VERSION=$(java -version 2>&1 | head -n 1 | grep -oP '\d+' | head -n 1)
if [ "$JAVA_VERSION" != "21" ]; then
  echo "❌ ERROR: Java $JAVA_VERSION detected, but Java 21 is required!"
  echo "Available Java versions:"
  update-alternatives --display java
  exit 1
fi

echo "✅ Java 21 confirmed"

# Start server in screen with optimized JVM flags
echo "🚀 Launching Paper server..."
screen -dmS minecraft java -Xmx6G -Xms12G \
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
  -jar paper.jar --nogui

echo "⏳ Waiting for server to start..."
sleep 50

# Verify server is running
if screen -list | grep -q "minecraft"; then
  echo "✅ Server process started successfully!"
  
  # Wait for server to be fully loaded
  echo "⏳ Waiting for world to load..."
  for i in {1..60}; do
    if [ -f logs/latest.log ]; then
      if grep -q "Done" logs/latest.log; then
        echo "✅ Server fully loaded!"
        break
      fi
    fi
    
    if [ $i -eq 60 ]; then
      echo "⚠️ Server took longer than expected to load"
      echo "📋 Last 10 lines of log:"
      tail -10 logs/latest.log 2>/dev/null || echo "No logs yet"
    fi
    
    sleep 2
  done
else
  echo "❌ Server failed to start!"
  echo "📋 Checking logs..."
  cat logs/latest.log 2>/dev/null || echo "No logs found"
  exit 1
fi

cd ..
