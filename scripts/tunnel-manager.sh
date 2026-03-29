#!/bin/bash
set -e

case "$1" in
  start)
    echo "🚀 Starting Playit.gg tunnel..."
    
    # Remove old container if exists
    docker stop playit 2>/dev/null || true
    docker rm playit 2>/dev/null || true
    
    # Start new tunnel
    docker run -d --name playit \
      --net=host \
      -e SECRET_KEY=${PLAYIT_SECRET_KEY} \
      ghcr.io/playit-cloud/playit-agent:0.17
    
    echo "⏳ Waiting for tunnel to establish..."
    sleep 15
    
    # Show tunnel info
    docker logs playit
    
    echo "✅ Playit tunnel started!"
    echo "🌐 Check https://playit.gg/account/tunnels for your address"
    ;;
    
  stop)
    echo "🛑 Stopping Playit tunnel..."
    docker stop playit || true
    docker rm playit || true
    echo "✅ Tunnel stopped"
    ;;
    
  restart)
    echo "🔄 Restarting Playit tunnel..."
    $0 stop
    sleep 2
    $0 start
    ;;
    
  *)
    echo "Usage: $0 {start|stop|restart}"
    exit 1
    ;;
esac
