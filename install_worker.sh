#!/bin/bash

echo "🚀 Installing VPS Worker Node..."

# Download worker script
wget -O /root/worker.py https://raw.githubusercontent.com/swapiolds/free-vps/main/worker.py

# Install dependency
apt-get update -y
apt-get install -y python3 python3-pip
pip3 install aiohttp psutil --break-system-packages || pip3 install aiohttp psutil || pip install aiohttp psutil

# Create SystemD Service
cat <<EOF > /etc/systemd/system/vpsworker.service
[Unit]
Description=VPS Worker Node
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/worker.py --master https://labored-swirl-reward.ngrok-free.dev
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Reload and Enable Service
systemctl daemon-reload
systemctl enable vpsworker
systemctl start vpsworker

echo "✅ Worker Node Installed and Running in Background!"
echo "It will automatically start even if the VPS reboots."
echo "To check status, run: systemctl status vpsworker"
