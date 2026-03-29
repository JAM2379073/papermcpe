#!/bin/bash
set -e

echo "🎮 Starting Minecraft server..."

cd minecraft-server

# Verify Java version
echo "☕ Using Java:"
java -version 2>&1 | head -n 1

# Check paper.jar exists
if [ ! -f paper.jar ]; then
  echo "❌ paper.jar not found!"
  exit 1
fi

echo "✅ Found paper.jar ($(du -h paper.jar | cut -f1))"

# Create logs directory
mkdir -p logs

# Start server with simple flags (6GB min, 12GB max)
echo "🚀 Starting server with 6GB-12GB RAM..."

screen -dmS minecraft java -Xms6G -Xmx12G -jar paper.jar --nogui

echo "⏳ Waiting for server to start..."
sleep 60

# Check if server is running
if screen -list | grep -q "minecraft"; then
  echo "✅ Server is running!"
  
  # Wait for "Done" in logs
  for i in {1..60}; do
    if [ -f logs/latest.log ] && grep -q "Done" logs/latest.log; then
      echo "✅ Server fully loaded!"
      tail -5 logs/latest.log
      cd ..
      exit 0
    fi
    sleep 2
  done
  
  echo "⚠️ Server running but not fully loaded yet"
  if [ -f logs/latest.log ]; then
    tail -10 logs/latest.log
  fi
else
  echo "❌ Server failed to start!"
  if [ -f logs/latest.log ]; then
    cat logs/latest.log
  fi
  exit 1
fi

cd ..
