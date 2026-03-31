#!/usr/bin/env python3
import os
import sys
import subprocess
from huggingface_hub import HfApi, hf_hub_download
from datetime import datetime

api = HfApi()
token = os.environ.get('HF_TOKEN')
repo_id = os.environ.get('HF_DATASET_REPO')

def download_backup():
    """Download backup from HuggingFace (server data + panel database)"""
    print("📥 Downloading backup from HuggingFace...")
    
    try:
        filepath = hf_hub_download(
            repo_id=repo_id,
            filename="server-backup.tar.gz",
            repo_type="dataset",
            token=token,
            local_dir="."
        )
        print(f"✅ Backup downloaded: {filepath}")
        
        # Extract backup - includes server data + panel.db
        print("📦 Extracting backup...")
        result = subprocess.run(
            ["tar", "-xzf", "server-backup.tar.gz"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            print("✅ Backup extracted successfully")
        else:
            print(f"⚠️ Extraction warning: {result.stderr}")
        
        return True
    except Exception as e:
        print(f"⚠️ No backup found: {e}")
        print("Starting fresh server...")
        return False

def upload_backup():
    """Create and upload backup to HuggingFace (server data + panel database)"""
    print("📦 Creating backup archive...")
    
    # Create backup including server data AND panel database
    # This ensures users, servers, schedules, and permissions persist across restarts
    os.chdir("minecraft-server")
    result = subprocess.run(
        ["tar", "-czf", "../server-backup-new.tar.gz",
            "--exclude=*.jar",
            "--exclude=logs/*",
            "--exclude=cache/*",
            "world/",
            "world_nether/",
            "world_the_end/",
            "plugins/*/config.yml",
            "server.properties",
            "bukkit.yml",
            "spigot.yml",
            "config/",
            "ops.json",
            "whitelist.json",
            "banned-players.json",
            "banned-ips.json",
            "usercache.json",
            "permissions.yml",
            "../scripts/panel/panel.db",
            "../scripts/panel/schedules.json"],
        capture_output=True, text=True, timeout=300
    )
    os.chdir("..")
    
    if result.returncode != 0:
        print(f"⚠️ Backup warning (some files may be missing): {result.stderr}")
    
    # Check backup was created
    if not os.path.exists("server-backup-new.tar.gz"):
        print("❌ Failed to create backup archive")
        return False
    
    # Get backup size
    size = os.path.getsize("server-backup-new.tar.gz")
    size_mb = size / (1024 * 1024)
    print(f"✅ Backup created: {size_mb:.2f} MB")
    
    # Save size to env if available
    github_env = os.environ.get('GITHUB_ENV')
    if github_env:
        with open(github_env, 'a') as f:
            f.write(f"BACKUP_SIZE={size_mb:.1f}M\n")
    
    # Upload to HuggingFace
    print("📤 Uploading to HuggingFace...")
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_number = os.environ.get('RUN_NUMBER', 'unknown')
    
    try:
        api.upload_file(
            path_or_fileobj="server-backup-new.tar.gz",
            path_in_repo="server-backup.tar.gz",
            repo_id=repo_id,
            repo_type="dataset",
            token=token,
            commit_message=f"Auto backup - Run #{run_number} - {timestamp}"
        )
        print("✅ Backup uploaded successfully!")
        return True
    except Exception as e:
        print(f"❌ Upload failed: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: backup-manager.py {download|upload}")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "download":
        download_backup()
    elif command == "upload":
        if not upload_backup():
            sys.exit(1)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
