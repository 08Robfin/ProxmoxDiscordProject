# 🚀 Proxmox Discord Manager

A Python bot to manage your Proxmox VMs and Containers directly from Discord. No VPN required to check status or boot machines.

## 🛠️ Setup
1. **Install:** `pip install -r requirements.txt`
2. **Configure:** Create a `.env` file with your Discord/Proxmox tokens.
3. **Start:** Run `python3 bot.py`

## 📋 Commands (More may be added later)
* `/sync` - Manually force a sync between Proxmox and the discord bot. (The system will usually sync automatically every ~30 seconds.)
* `/start` - Powers on the VM and updates channel to 🟢.
* `/stop` - Powers off the VM and updates channel to 🔴.

## 📂 Project Structure
* `bot.py` - Main bot logic and Proxmox API connection.
* `.env` - (Hidden) API keys and secrets.
* `.gitignore` - Prevents secrets and venv from being pushed to GitHub.
* `requirements.txt` - List of necessary Python libraries.
