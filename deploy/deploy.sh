#!/bin/bash
# AlgoForge EC2 Deployment Script
# Run this once on a fresh Ubuntu 22.04 t3.micro
# Usage: bash deploy.sh YOUR_ELASTIC_IP

set -e
ELASTIC_IP=${1:-"YOUR_ELASTIC_IP"}
APP_DIR="/home/ubuntu/algoforge"

echo "==> Updating system packages..."
sudo apt-get update -y && sudo apt-get upgrade -y

echo "==> Installing Python 3.11, nginx, git..."
sudo apt-get install -y python3.11 python3.11-venv python3-pip nginx git

echo "==> Cloning / pulling repo..."
if [ -d "$APP_DIR" ]; then
  cd "$APP_DIR" && git pull
else
  git clone https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPO.git "$APP_DIR"
  cd "$APP_DIR"
fi

echo "==> Creating virtual environment..."
python3.11 -m venv venv
source venv/bin/activate

echo "==> Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Copying .env (if not already present)..."
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "!! IMPORTANT: Edit $APP_DIR/.env and add your Dhan credentials before starting the service"
fi

echo "==> Installing systemd service..."
sudo cp "$APP_DIR/deploy/algoforge.service" /etc/systemd/system/algoforge.service
sudo systemctl daemon-reload
sudo systemctl enable algoforge
sudo systemctl restart algoforge

echo "==> Configuring nginx..."
sudo sed "s/YOUR_ELASTIC_IP/$ELASTIC_IP/g" "$APP_DIR/deploy/nginx.conf" \
  > /tmp/algoforge_nginx.conf
sudo cp /tmp/algoforge_nginx.conf /etc/nginx/sites-available/algoforge
sudo ln -sf /etc/nginx/sites-available/algoforge /etc/nginx/sites-enabled/algoforge
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

echo ""
echo "==> DONE. AlgoForge is running at http://$ELASTIC_IP"
echo "    Check service status: sudo systemctl status algoforge"
echo "    Check logs:           sudo journalctl -u algoforge -f"
