#!/bin/bash

# --- Configuration Variables ---
# The project will be installed from the current directory where this script resides.
PROJECT_DIR_NAME="hat_detector_web_app_local" # A new name to represent its local origin
INSTALL_PATH="/opt/$PROJECT_DIR_NAME" # Where the project will be installed on the VM
SERVICE_PORT="80" # Default HTTP port (80)

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
   exec sudo bash "<span class="math-inline">0" "</span>@" # Re-execute the script with sudo
   exit # Should not be reached
fi

echo "--- Starting Hat Detector Web App Installation ---"

# Determine the user and group for the systemd service based on who ran sudo
WEB_SERVICE_USER="<span class="math-inline">\{SUDO\_USER\:\-root\}" \# Default to root if SUDO\_USER is not set
WEB\_SERVICE\_GROUP\=</span>(id -gn "$WEB_SERVICE_USER" 2>/dev/null || echo "nogroup") # Get primary group, default to nogroup if user not found

echo "Service will attempt to run as user: $WEB_SERVICE_USER, group: $WEB_SERVICE_GROUP"

# --- 1. Update System Packages ---
echo "1. Updating system packages..."
apt update && apt upgrade -y || { echo "Failed to update packages. Exiting."; exit 1; }

# --- 2. Install Needed OS Dependencies ---
echo "2. Installing OS dependencies (Python, Git, OpenCV libs, Gunicorn, libcap2-bin)..."
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

# --- 3. Copy Project Files to Install Path ---
echo "3. Copying project files to <span class="math-inline">INSTALL\_PATH\.\.\."
\# Get the directory where the install script itself is located
SCRIPT\_DIR\="</span>( cd "<span class="math-inline">\( dirname "</span>{BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_SOURCE_DIR="$SCRIPT_DIR/hat_detector_web" # Assuming the 'hat_detector_web' folder is next to the script

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

# Ensure the correct user owns the project directory for virtual env setup
chown -R "$WEB_SERVICE_USER":"$WEB_SERVICE_GROUP" "$INSTALL_PATH" || { echo "Failed to change ownership of $INSTALL_PATH. Exiting."; exit 1; }

# --- 4. Setup Python Virtual Environment and Install Python Dependencies ---
echo "4. Setting up Python virtual environment and installing dependencies..."
sudo -u "$WEB_SERVICE_USER" python3 -m venv "$INSTALL_PATH/venv" || { echo "Failed to create virtual environment. Exiting."; exit 1; }
sudo -u "$WEB_SERVICE_USER" bash -c "source $INSTALL_PATH/venv/bin/activate && pip install -r $INSTALL_PATH/requirements.txt" || { echo "Failed to install Python dependencies. Exiting."; exit 1; }

# --- 5. Configure Gunicorn for privileged ports ---
echo "5. Configuring Gunicorn for privileged ports (setcap)..."
GUNICORN_BIN="$INSTALL_PATH/venv/bin/gunicorn"
if [ -f "$GUNICORN_BIN" ]; then
    chmod +x "$GUNICORN_BIN"
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

Environment="VISION_ENDPOINT=$AZURE_VISION_ENDPOINT"
Environment="VISION_KEY=$AZURE_VISION_KEY"

ExecStart=$GUNICORN_BIN --workers 1 --bind 0.0.0.0:<span class="math-inline">SERVICE\_PORT app\:app
ReadWritePaths\=/dev/video0
StandardOutput\=syslog
StandardError\=syslog
SyslogIdentifier\=hat\-detector
Restart\=always
RestartSec\=10
\[Install\]
WantedBy\=multi\-user\.target
EOF
\# \-\-\- 7\. Set Webcam Device Permissions \(if needed\) \-\-\-
echo "7\. Setting webcam device permissions \(if /dev/video0 exists\)\.\.\."
if \[ \-c /dev/video0 \]; then
VIDEO\_GID\=</span>(grep video /etc/group | cut -d: -f3)
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

# --- 8. Configure Firewall (UFW for Ubuntu, Firewalld for CentOS/Fedora) ---
echo "8. Configuring firewall to allow port $SERVICE_PORT..."
if command -v ufw &>
