#!/bin/bash
set -e

echo "🔧 Setting up Minecraft server..."

mkdir -p minecraft-server
cd minecraft-server

# Restore backup if exists
if [ -f ../server-backup.tar.gz ]; then
  echo "📦 Restoring from backup..."
  tar -xzf ../server-backup.tar.gz
  echo "✅ Backup restored!"
else
  echo "🆕 Fresh server installation"
fi

# Download Paper Server
if [ ! -f paper.jar ]; then
  echo "⬇️ Downloading Paper 1.21.1..."
  wget -q --show-progress -O paper.jar \
    "https://api.papermc.io/v2/projects/paper/versions/1.21.1/builds/127/downloads/paper-1.21.1-127.jar"
fi

# Create plugins directory
mkdir -p plugins

# Download ViaVersion
if [ ! -f plugins/ViaVersion-5.8.0.jar ]; then
  echo "⬇️ Downloading ViaVersion..."
  wget -q --show-progress -O plugins/ViaVersion-5.8.0.jar \
    "https://cdn.modrinth.com/data/P1OZGk5p/versions/jVKER2UB/ViaVersion-5.8.0.jar"
fi

# Download ViaBackwards
if [ ! -f plugins/ViaBackwards-5.8.0.jar ]; then
  echo "⬇️ Downloading ViaBackwards..."
  wget -q --show-progress -O plugins/ViaBackwards-5.8.0.jar \
    "https://cdn.modrinth.com/data/NpvuJQoq/versions/MGpJckRt/ViaBackwards-5.8.0.jar"
fi

# Download Geyser
if [ ! -f plugins/Geyser-Spigot.jar ]; then
  echo "⬇️ Downloading Geyser..."
  wget -q --show-progress -O plugins/Geyser-Spigot.jar \
    "https://cdn.modrinth.com/data/wKkoqHrH/versions/VqOInBgb/Geyser-Spigot.jar"
fi

# Download Floodgate
if [ ! -f plugins/floodgate-spigot.jar ]; then
  echo "⬇️ Downloading Floodgate..."
  wget -q --show-progress -O plugins/floodgate-spigot.jar \
    "https://download.geysermc.org/v2/projects/floodgate/versions/latest/builds/latest/downloads/spigot"
fi

# Accept EULA
echo "eula=true" > eula.txt

# Create server.properties
if [ ! -f server.properties ]; then
  cat > server.properties << 'EOF'
motd=§6⚡ Paper Server §8| §bPlayit.gg §8| §aAuto-Loop
server-port=25565
max-players=20
difficulty=normal
gamemode=survival
pvp=true
online-mode=false
enable-command-block=true
spawn-protection=0
view-distance=10
simulation-distance=8
max-world-size=29999984
white-list=false
allow-flight=true
EOF
fi

cd ..
echo "✅ Server setup complete!"
