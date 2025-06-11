#!/bin/bash

# --- Configuration Variables (Edit these if you don't provide them as arguments) ---
REPO_URL="https://github.com/your-username/hat_detector_web.git" # IMPORTANT: Replace with your actual Git repo URL
PROJECT_DIR_NAME="hat_detector_web" # Name of the directory your repo clones into
INSTALL_PATH="/opt/$PROJECT_DIR_NAME" # Where the project will be installed on the VM
SERVICE_PORT="80" # Changed to default HTTP port (80)
# WEB_SERVICE_USER and WEB_SERVICE_GROUP will be determined dynamically
# based on who runs the script with sudo, then explicitly set for systemd.

# --- Azure Credentials (MUST be provided as arguments or hardcoded here) ---
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
# If script was run with 'sudo script.sh', SUDO_USER is the original user.
# If script was run as 'sudo su -' then 'script.sh', SUDO_USER might be empty.
# In that case, we'll use 'root' or ensure a specific user exists.
WEB_SERVICE_USER="${SUDO_USER:-root}" # Default to root if SUDO_USER is not set
WEB_SERVICE_GROUP=$(id -gn "$WEB_SERVICE_USER" 2>/dev/null || echo "nogroup") # Get primary group, default to nogroup if user not found

echo "Service will attempt to run as user: $WEB_SERVICE_USER, group: $WEB_SERVICE_GROUP"

# --- 1. Update System Packages ---
echo "1. Updating system packages..."
apt update && apt upgrade -y || { echo "Failed to update packages. Exiting."; exit 1; }

# --- 2. Install Needed OS Dependencies ---
echo "2. Installing OS dependencies (Python, Git, OpenCV libs, Gunicorn, libcap2-bin)..."
# libcap2-bin provides setcap
apt install -y \
    python3 python3-pip python3-venv \
    git \
    libgl1 \
    libjpeg-dev libpng-dev libtiff-dev \
    libavcodec-dev libavformat-dev libswscale-dev \
    libv4l-dev \
    libxvidcore-dev libx264-dev \
    gunicorn \
    libcap2-bin || { echo "Failed to install OS dependencies. Exiting."; exit 1; }

# --- 3. Clone / Navigate to Project Directory ---
echo "3. Setting up project directory..."
if [ -d "$INSTALL_PATH" ]; then
    echo "Project directory already exists. Pulling latest changes..."
    cd "$INSTALL_PATH" || { echo "Failed to change directory to $INSTALL_PATH. Exiting."; exit 1; }
    # Ensure we are on the main branch before pulling, or specify branch
    git checkout main || git checkout master || echo "Could not checkout main/master branch, continuing with current branch."
    git pull || { echo "Failed to pull latest changes. Continuing anyway, but check Git status."; }
else
    echo "Cloning project repository into $INSTALL_PATH..."
    git clone "$REPO_URL" "$INSTALL_PATH" || { echo "Failed to clone repository. Exiting."; exit 1; }
    cd "$INSTALL_PATH" || { echo "Failed to change directory to $INSTALL_PATH. Exiting."; exit 1; }
fi

# Ensure the correct user owns the project directory for virtual env setup
chown -R "$WEB_SERVICE_USER":"$WEB_SERVICE_GROUP" "$INSTALL_PATH" || { echo "Failed to change ownership of $INSTALL_PATH. Exiting."; exit 1; }

# --- 4. Setup Python Virtual Environment and Install Python Dependencies ---
echo "4. Setting up Python virtual environment and installing dependencies..."
# Execute venv creation and pip install as the WEB_SERVICE_USER
sudo -u "$WEB_SERVICE_USER" python3 -m venv "$INSTALL_PATH/venv" || { echo "Failed to create virtual environment. Exiting."; exit 1; }
sudo -u "$WEB_SERVICE_USER" bash -c "source $INSTALL_PATH/venv/bin/activate && pip install -r $INSTALL_PATH/requirements.txt" || { echo "Failed to install Python dependencies. Exiting."; exit 1; }

# --- 5. Configure Gunicorn for privileged ports ---
# This allows Gunicorn to bind to port 80 as a non-root user
echo "5. Configuring Gunicorn for privileged ports (setcap)..."
GUNICORN_BIN="$INSTALL_PATH/venv/bin/gunicorn"
if [ -f "$GUNICORN_BIN" ]; then
    # Ensure gunicorn binary is executable
    chmod +x "$GUNICORN_BIN"
    # Set the CAP_NET_BIND_SERVICE capability
    setcap 'cap_net_bind_service=+ep' "$GUNICORN_BIN" || { echo "Warning: Failed to setcap on Gunicorn binary. Port 80 might not work as non-root."; }
else
    echo "Warning: Gunicorn binary not found at $GUNICORN_BIN. Setcap skipped."
fi


# --- 6. Configure systemd Service ---
echo "6. Creating systemd service file..."
SERVICE_FILE="/etc/systemd/system/hat-detector.service"

cat <<EOF | tee "$SERVICE_FILE"
[Unit]
Description=Hat Detector Web App
After=network.target multi-user.target

[Service]
User=$WEB_SERVICE_USER
Group=$WEB_SERVICE_GROUP
WorkingDirectory=$INSTALL_PATH

# Set environment variables for Azure credentials
Environment="VISION_ENDPOINT=$AZURE_VISION_ENDPOINT"
Environment="VISION_KEY=$AZURE_VISION_KEY"

# Command to execute (activate venv and run gunicorn)
# Gunicorn will bind to port 80. setcap configured in step 5 handles permissions.
ExecStart=$GUNICORN_BIN --workers 1 --bind 0.0.0.0:$SERVICE_PORT app:app

# Permissions for webcam (if using one)
# This allows the service to access /dev/video0.
# The user ($WEB_SERVICE_USER) also needs to be in the 'video' group.
ReadWritePaths=/dev/video0

# Standard output and error to syslog
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=hat-detector

# Optional: Restart the service if it stops unexpectedly
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# --- 7. Set Webcam Device Permissions (if needed) ---
# This part is crucial if the web service user doesn't have direct access to /dev/video0
echo "7. Setting webcam device permissions (if /dev/video0 exists)..."
if [ -c /dev/video0 ]; then # Check if /dev/video0 is a character device
    VIDEO_GID=$(grep video /etc/group | cut -d: -f3)
    if [ -n "$VIDEO_GID" ]; then
        echo "Found video group GID: $VIDEO_GID."
        # Add the service user to the 'video' group
        usermod -aG video "$WEB_SERVICE_USER" || { echo "Failed to add user '$WEB_SERVICE_USER' to video group."; }
        echo "Please ensure the VM has access to /dev/video0 and it's visible. A VM reboot might be required for full device permissions to apply to the user."
    else
        echo "Warning: 'video' group not found. Webcam access might fail without proper permissions."
    fi
else
    echo "No /dev/video0 found. Assuming no webcam is connected or needed."
fi

# --- 8. Configure Firewall (UFW for Ubuntu, Firewalld for CentOS/Fedora) ---
echo "8. Configuring firewall to allow port $SERVICE_PORT..."
if command -v ufw &> /dev/null; then
    ufw allow "$SERVICE_PORT"/tcp || echo "Warning: Failed to allow port $SERVICE_PORT in UFW. Check manually."
    ufw reload || echo "Warning: Failed to reload UFW. Check manually."
    ufw enable || echo "Warning: UFW not enabled. Enabling it might block other services. Check manually."
elif command -v firewall-cmd &> /dev/null; then
    firewall-cmd --add-port="$SERVICE_PORT"/tcp --permanent || echo "Warning: Failed to add port $SERVICE_PORT in Firewalld. Check manually."
    firewall-cmd --reload || echo "Warning: Failed to reload Firewalld. Check manually."
else
    echo "Warning: No UFW or Firewalld found. Please configure your firewall manually if needed."
fi


# --- 9. Enable and Start the Service ---
echo "9. Reloading systemd daemon, enabling and starting service..."
systemctl daemon-reload || { echo "Failed to reload systemd daemon. Exiting."; exit 1; }
systemctl enable hat-detector || { echo "Failed to enable hat-detector service. Exiting."; exit 1; }

# Stop any running instances first, then start fresh
systemctl stop hat-detector # It's okay if this fails the first time or if service isn't running
systemctl start hat-detector || { echo "Failed to start hat-detector service. Check 'sudo journalctl -u hat-detector'. Exiting."; exit 1; }

echo "--- Installation Complete ---"
echo "Web service should be running on the default HTTP port (80)."
echo "Access it via: http://<VM_IP_Address>" # No port needed in URL
echo "Check service status: sudo systemctl status hat-detector"
echo "View service logs: sudo journalctl -u hat-detector -f"
echo "It is recommended to reboot the VM (sudo reboot) after installation for full webcam permissions to take effect."
