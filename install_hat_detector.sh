#!/bin/bash

# --- Configuration Variables ---
PROJECT_DIR_NAME="hat_detector_web_app_local"
INSTALL_PATH="/opt/$PROJECT_DIR_NAME"
GUNICORN_PORT="8000" # Gunicorn will now bind to a non-privileged port

# --- Azure Credentials (MUST be provided as arguments) ---
AZURE_VISION_ENDPOINT=""
AZURE_VISION_KEY=""

# --- Check for Azure arguments ---
if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Usage: $0 <AZURE_VISION_ENDPOINT> <AZURE_VISION_KEY>"
    echo "Example: $0 \"https://myresource.cognitiveservices.azure.com/\" \"YOUR_32_CHAR_KEY\""
    echo "Azure credentials are required. Exiting."
    exit 1
else
    AZURE_VISION_ENDPOINT="$1"
    AZURE_VISION_KEY="$2"
    echo "Azure credentials provided."
fi

# --- Check if running as root (needed for system installs) ---
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root or with sudo."
   exec sudo bash "$0" "$@" # Re-execute the script with sudo
   exit # Should not be reached
fi

echo "--- Starting Hat Detector Web App Installation ---"

# Determine the user and group for the systemd service based on who ran sudo
WEB_SERVICE_USER="${SUDO_USER:-root}" # Default to root if SUDO_USER is not set
WEB_SERVICE_GROUP=$(id -gn "$WEB_SERVICE_USER" 2>/dev/null || echo "nogroup") # Get primary group, default to nogroup if user not found

echo "Service will attempt to run as user: $WEB_SERVICE_USER, group: $WEB_SERVICE_GROUP"

# --- 1. Update System Packages ---
echo "1. Updating system packages..."
apt update && apt upgrade -y || { echo "Failed to update packages. Exiting."; exit 1; }

# --- 2. Install Needed OS Dependencies ---
echo "2. Installing OS dependencies (Python, Git, OpenCV libs, Gunicorn, Nginx, libcap2-bin)..."
apt install -y \
    python3 python3-pip python3-venv \
    git \
    libgl1 \
    libjpeg-dev libpng-dev libtiff-dev \
    libavcodec-dev libavformat-dev libswscale-dev \
    libv4l-dev \
    libxvidcore-dev libx264-dev \
    gunicorn \
    nginx \
    libcap2-bin || { echo "Failed to install OS dependencies. Exiting."; exit 1; }

# --- 3. Copy Project Files to Install Path ---
echo "3. Copying project files to $INSTALL_PATH..."
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_SOURCE_DIR="$SCRIPT_DIR/hat_detector_web"

if [ ! -d "$PROJECT_SOURCE_DIR" ]; then
    echo "Error: Application source directory '$PROJECT_SOURCE_DIR' not found."
    echo "Please ensure the 'hat_detector_web' folder is in the same directory as this script."
    exit 1
fi

if [ -d "$INSTALL_PATH" ]; then
    echo "Existing installation found at $INSTALL_PATH. Removing old files..."
    rm -rf "$INSTALL_PATH" || { echo "Failed to remove old installation. Exiting."; exit 1; }
fi

cp -r "$PROJECT_SOURCE_DIR" "$INSTALL_PATH" || { echo "Failed to copy project files to $INSTALL_PATH. Exiting."; exit 1; }
echo "Project files copied successfully."
cd "$INSTALL_PATH" || { echo "Failed to change directory to $INSTALL_PATH. Exiting."; exit 1; }

chown -R "$WEB_SERVICE_USER":"$WEB_SERVICE_GROUP" "$INSTALL_PATH" || { echo "Failed to change ownership of $INSTALL_PATH. Exiting."; exit 1; }

# --- 4. Setup Python Virtual Environment and Install Python Dependencies ---
echo "4. Setting up Python virtual environment and installing dependencies..."
sudo -u "$WEB_SERVICE_USER" python3 -m venv "$INSTALL_PATH/venv" || { echo "Failed to create virtual environment. Exiting."; exit 1; }
sudo -u "$WEB_SERVICE_USER" bash -c "source $INSTALL_PATH/venv/bin/activate && pip install -r $INSTALL_PATH/requirements.txt" || { echo "Failed to install Python dependencies. Exiting."; exit 1; }

# --- 5. Clean up setcap (no longer needed) ---
echo "5. Removing setcap from Gunicorn (no longer needed for non-privileged port)..."
GUNICORN_BIN="$INSTALL_PATH/venv/bin/gunicorn"
if [ -f "$GUNICORN_BIN" ]; then
    sudo setcap -r "$GUNICORN_BIN" || echo "Warning: Failed to remove capabilities from Gunicorn binary. Proceeding."
else
    echo "Warning: Gunicorn binary not found at $GUNICORN_PATH. Skipping setcap removal."
fi

# --- 6. Configure systemd Service for Gunicorn (on high port) ---
echo "6. Creating systemd service file for Gunicorn..."
SERVICE_FILE="/etc/systemd/system/hat-detector.service"

cat <<EOF | tee "$SERVICE_FILE"
[Unit]
Description=Hat Detector Web App
After=network.target multi-user.target

[Service]
User=$WEB_SERVICE_USER
Group=$WEB_SERVICE_GROUP
WorkingDirectory=$INSTALL_PATH

Environment="VISION_ENDPOINT=$AZURE_VISION_ENDPOINT"
Environment="VISION_KEY=$AZURE_VISION_KEY"

# Gunicorn now binds to a non-privileged port, Nginx will proxy to it
ExecStart=$INSTALL_PATH/venv/bin/gunicorn --workers 1 --bind 0.0.0.0:$GUNICORN_PORT app:app

ReadWritePaths=/dev/video0

StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=hat-detector

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# --- 7. Configure Nginx as a Reverse Proxy ---
echo "7. Configuring Nginx as a reverse proxy..."
NGINX_CONF_AVAILABLE="/etc/nginx/sites-available/hat-detector"
NGINX_CONF_ENABLED="/etc/nginx/sites-enabled/hat-detector"

cat <<EOF | sudo tee "$NGINX_CONF_AVAILABLE"
server {
    listen 80;
    server_name _; # Listen on all available IPs

    location / {
        proxy_pass http://127.0.0.1:$GUNICORN_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Optionally, serve static files directly with Nginx (more efficient)
    # This assumes your static files are in /opt/hat_detector_web_app_local/static
    location /static/ {
        alias $INSTALL_PATH/static/;
        expires 30d; # Cache static files for 30 days
        add_header Cache-Control "public, no-transform";
    }
}
EOF

# Enable the Nginx site
if [ -f "$NGINX_CONF_ENABLED" ]; then
    rm "$NGINX_CONF_ENABLED" || echo "Warning: Failed to remove old Nginx symlink."
fi
ln -s "$NGINX_CONF_AVAILABLE" "$NGINX_CONF_ENABLED" || { echo "Failed to create Nginx symlink. Exiting."; exit 1; }

# Test Nginx configuration and restart
sudo nginx -t && sudo systemctl restart nginx || { echo "Nginx configuration test failed or restart failed. Exiting."; exit 1; }
echo "Nginx configured and restarted successfully."

# --- 8. Set Webcam Device Permissions (if needed) ---
echo "8. Setting webcam device permissions (if /dev/video0 exists)..."
if [ -c /dev/video0 ]; then
    VIDEO_GID=$(grep video /etc/group | cut -d: -f3)
    if [ -n "$VIDEO_GID" ]; then
        echo "Found video group GID: $VIDEO_GID."
        usermod -aG video "$WEB_SERVICE_USER" || { echo "Failed to add user '$WEB_SERVICE_USER' to video group."; }
        echo "Please ensure the VM has access to /dev/video0 and it's visible. A VM reboot might be required for full device permissions to apply to the user."
    else
        echo "Warning: 'video' group not found. Webcam access might fail without proper permissions."
    fi
else
    echo "No /dev/video0 found. Assuming no webcam is connected or needed."
fi

# --- 9. Configure Firewall (UFW for Ubuntu, Firewalld for CentOS/Fedora) ---
echo "9. Configuring firewall to allow port 80..."
if command -v ufw &> /dev/null; then
    ufw allow 80/tcp || echo "Warning: Failed to allow port 80 in UFW. Check manually."
    ufw reload || echo "Warning: Failed to reload UFW. Check manually."
    ufw enable || echo "Warning: UFW not enabled. Enabling it might block other services. Check manually."
elif command -v firewall-cmd &> /dev/null; then
    firewall-cmd --add-port=80/tcp --permanent || echo "Warning: Failed to add port 80 in Firewalld. Check manually."
    firewall-cmd --reload || echo "Warning: Failed to reload Firewalld. Check manually."
else
    echo "Warning: No UFW or Firewalld found. Please configure your firewall manually if needed."
fi

# --- 10. Enable and Start the Hat Detector Service ---
echo "10. Reloading systemd daemon, enabling and starting Hat Detector service..."
systemctl daemon-reload || { echo "Failed to reload systemd daemon. Exiting."; exit 1; }
systemctl enable hat-detector || { echo "Failed to enable hat-detector service. Exiting."; exit 1; }

systemctl stop hat-detector # Ensure it's stopped before starting afresh
systemctl start hat-detector || { echo "Failed to start hat-detector service. Check 'sudo journalctl -u hat-detector'. Exiting."; exit 1; }

echo "--- Installation Complete ---"
echo "Web service should now be accessible via Nginx on port 80."
echo "Access it via: http://<VM_IP_Address>"
echo "Check Gunicorn service status: sudo systemctl status hat-detector"
echo "View Gunicorn service logs: sudo journalctl -u hat-detector -f"
echo "Check Nginx service status: sudo systemctl status nginx"
echo "View Nginx access logs: tail -f /var/log/nginx/access.log"
echo "View Nginx error logs: tail -f /var/log/nginx/error.log"
echo "It is recommended to reboot the VM (sudo reboot) after installation for full webcam permissions to take effect."
