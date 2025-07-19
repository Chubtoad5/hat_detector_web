#!/bin/bash

# =============================================================================
# All-in-One Deployment Script for the Hat Detector Application
# =============================================================================

# --- Script Configuration ---
set -e # Exit immediately if a command exits with a non-zero status.

# =============================================================================
# CONFIGURATION - EDIT THESE VARIABLES
# =============================================================================
# --- Azure Computer Vision Credentials ---
AZURE_VISION_KEY="YOUR_AZURE_KEY_HERE"
AZURE_VISION_ENDPOINT="YOUR_AZURE_ENDPOINT_HERE"

# --- RTSP Stream URL ---
# Example: "rtsp://admin:password@192.168.1.100/stream1"
# Example for secure stream: "rtsp://192.168.1.1:7441/L0DUQ6167DCp9BE3"
RTSP_URL="rtsp://192.168.1.1:7441/L0DUQ6167DCp9BE"

# --- Application User and Directory ---
APP_USER="hat"
APP_DIR="/opt/hat_detector_web_app"
# =============================================================================
# END OF CONFIGURATION
# =============================================================================

# --- Check for Root ---
if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run as root. Please use sudo." >&2
  exit 1
fi

# --- Check if variables have been configured ---
if [[ "$AZURE_VISION_KEY" == "YOUR_AZURE_KEY_HERE" || "$AZURE_VISION_ENDPOINT" == "YOUR_AZURE_ENDPOINT_HERE" ]]; then
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "!!! ERROR: Please edit this script and set your Azure    !!!"
  echo "!!!        credentials in the CONFIGURATION section.     !!!"
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  exit 1
fi

echo "--- [Step 1/8] Updating system and installing prerequisites... ---"
apt-get update
apt-get install -y python3-venv python3-pip nginx ffmpeg apparmor-utils

echo "--- [Step 2/8] Creating application user and directory... ---"
if id "$APP_USER" &>/dev/null; then
    echo "User '$APP_USER' already exists."
else
    useradd -r -s /bin/false $APP_USER
    echo "User '$APP_USER' created."
fi
# Add user to the 'video' group for camera access
usermod -a -G video $APP_USER
# Add the Nginx user to the app's group for socket access
usermod -a -G $APP_USER www-data

mkdir -p $APP_DIR/templates
mkdir -p $APP_DIR/static
# Set correct directory permissions for Nginx access
chmod 755 $APP_DIR
chown -R $APP_USER:$APP_USER $APP_DIR

echo "--- [Step 3/8] Creating application files... ---"

# --- requirements.txt ---
cat <<'EOF' > $APP_DIR/requirements.txt
Flask
gunicorn
opencv-python
numpy
azure-cognitiveservices-vision-computervision
msrest
tenacity
sdnotify
gevent
EOF
echo "Created requirements.txt"

# --- app.py ---
cat <<'EOF' > $APP_DIR/app.py
import gevent.monkey
gevent.monkey.patch_all()

import os
import cv2
import numpy as np
import time
import logging
import io
import subprocess
import queue
from flask import Flask, render_template, Response, jsonify, request
import multiprocessing.shared_memory as shared_memory
from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from msrest.authentication import CognitiveServicesCredentials
from concurrent.futures import ThreadPoolExecutor

# =============================================================================
# CONFIGURATION
# =============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(processName)s - %(message)s')
logger = logging.getLogger(__name__)
app = Flask(__name__)

# --- App Config ---
CONFIG_FILE_PATH = "/tmp/camera_source.txt"
SHM_NAME = 'hat_detector_frame_buffer' # Must match camera_manager.py

# --- Frame Config ---
FRAME_HEIGHT = 720
FRAME_WIDTH = 1280
FRAME_CHANNELS = 3

# --- Azure Config ---
COMPUTER_VISION_SUBSCRIPTION_KEY = os.environ.get("AZURE_VISION_SUBSCRIPTION_KEY")
COMPUTER_VISION_ENDPOINT = os.environ.get("AZURE_VISION_ENDPOINT")

# =============================================================================
# INITIALIZATION
# =============================================================================
computervision_client = None
analysis_executor = ThreadPoolExecutor(max_workers=1)
camera_unavailable_image_bytes = None

try:
    with open("static/camera_unavailable.jpg", "rb") as f:
        camera_unavailable_image_bytes = f.read()
except Exception as e:
    logger.error(f"Could not load placeholder image: {e}")

if COMPUTER_VISION_SUBSCRIPTION_KEY and COMPUTER_VISION_ENDPOINT:
    try:
        computervision_client = ComputerVisionClient(
            endpoint=COMPUTER_VISION_ENDPOINT,
            credentials=CognitiveServicesCredentials(COMPUTER_VISION_SUBSCRIPTION_KEY)
        )
        logger.info("Computer Vision client initialized.")
    except Exception as e:
        logger.error(f"Could not initialize Azure client: {e}")
else:
    logger.warning("Azure credentials not found. Analysis will be disabled.")

# =============================================================================
# FLASK ROUTES
# =============================================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    def generate_frames():
        existing_shm = None
        while True:
            try:
                if existing_shm is None:
                    existing_shm = shared_memory.SharedMemory(name=SHM_NAME)
                    logger.info("Web app connected to shared memory for video feed.")

                shared_frame_array = np.ndarray((FRAME_HEIGHT, FRAME_WIDTH, FRAME_CHANNELS), dtype=np.uint8, buffer=existing_shm.buf)
                frame = shared_frame_array.copy()

                ret, jpeg = cv2.imencode('.jpg', frame)
                if ret:
                    yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')

            except FileNotFoundError:
                if existing_shm:
                    existing_shm.close()
                    existing_shm = None
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + camera_unavailable_image_bytes + b'\r\n')
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in video feed generator: {e}")
                time.sleep(1)

            time.sleep(0.033)

    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/switch_camera', methods=['POST'])
def switch_camera():
    new_source = request.args.get('source', 'local').lower()
    if new_source not in ['local', 'rtsp']:
        return jsonify({"status": "error", "message": "Invalid source"}), 400

    logger.info(f"Request to switch camera to '{new_source}'")
    try:
        with open(CONFIG_FILE_PATH, "w") as f:
            f.write(new_source)

        command = ["sudo", "systemctl", "restart", "camera.service"]
        result = subprocess.run(command, capture_output=True, text=True, check=True)

        logger.info("Successfully triggered restart of camera.service")
        return jsonify({"status": "success", "message": f"Switched to {new_source} camera."})
    except subprocess.CalledProcessError as e:
        error_message = f"systemctl command failed with exit code {e.returncode}. Stderr: {e.stderr.strip()}"
        logger.error(error_message)
        return jsonify({"status": "error", "message": "Failed to restart camera service."}), 500
    except Exception as e:
        logger.error(f"An unexpected error occurred during camera switch: {e}")
        return jsonify({"status": "error", "message": "An unexpected error occurred."}), 500

@app.route('/analyze_current_frame')
def analyze_current_frame():
    if not computervision_client:
        return jsonify({"status": "error", "message": "Azure analysis client is not configured."}), 500

    shm = None
    try:
        shm = shared_memory.SharedMemory(name=SHM_NAME)
        shared_frame_array = np.ndarray((FRAME_HEIGHT, FRAME_WIDTH, FRAME_CHANNELS), dtype=np.uint8, buffer=shm.buf)
        frame_to_analyze = shared_frame_array.copy()
        shm.close()

        if not frame_to_analyze.any():
            return jsonify({"status": "error", "message": "Received an empty frame from camera."}), 400

        ret, jpeg_bytes = cv2.imencode('.jpg', frame_to_analyze)
        if not ret:
            return jsonify({"status": "error", "message": "Failed to encode frame for analysis."}), 500

        def run_analysis_task(frame_b, result_queue):
            try:
                analysis = computervision_client.analyze_image_in_stream(io.BytesIO(frame_b), ["Objects", "Tags", "Description"])
                data = {"description": analysis.description.as_dict() if analysis.description else {}, "tags": [t.as_dict() for t in analysis.tags] if analysis.tags else [], "objects": [], "hat_objects": []}
                if analysis.objects:
                    for obj in analysis.objects:
                        obj_dict = obj.as_dict()
                        data["objects"].append(obj_dict)
                        if obj.object_property and obj.object_property.lower() in ['hat', 'cap']:
                            data["hat_objects"].append(obj_dict)
                result_queue.put({"status": "completed", "analysis_data": data})
            except Exception as ex:
                result_queue.put({"status": "failed", "message": str(ex)})

        result_queue = queue.Queue()
        analysis_executor.submit(run_analysis_task, jpeg_bytes.tobytes(), result_queue)
        result = result_queue.get(timeout=20)

        if result['status'] == 'completed':
            return jsonify({"status": "success", "analysis_data": result['analysis_data']})
        else:
            return jsonify({"status": "error", "message": result['message']}), 500

    except FileNotFoundError:
        return jsonify({"status": "error", "message": "Camera feed not active (shared memory not found)."}), 404
    except queue.Empty:
        return jsonify({"status": "error", "message": "Analysis processing timed out."}), 504
    except Exception as e:
        logger.error(f"Error during analysis: {e}")
        return jsonify({"status": "error", "message": "An internal error occurred during analysis."}), 500
    finally:
        if shm:
            shm.close()
EOF
echo "Created app.py"

# --- camera_manager.py ---
cat <<'EOF' > $APP_DIR/camera_manager.py
import gevent.monkey
gevent.monkey.patch_all()

import cv2
import numpy as np
import time
import logging
import os
import multiprocessing.shared_memory as shared_memory
import signal
import sys

# =============================================================================
# CONFIGURATION
# =============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
RTSP_STREAM_URL = os.environ.get("RTSP_STREAM_URL")

# Set a long timeout (60 seconds) for FFMPEG to be more patient with streams
os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = 'rtsp_transport;tcp|timeout;60000000'

FRAME_HEIGHT = 720
FRAME_WIDTH = 1280
FRAME_CHANNELS = 3
SHARED_BUFFER_SIZE = FRAME_HEIGHT * FRAME_WIDTH * FRAME_CHANNELS
CONFIG_FILE_PATH = "/tmp/camera_source.txt"
SHM_NAME = 'hat_detector_frame_buffer'

# --- Global objects for cleanup ---
shm = None
camera = None

def cleanup(signum, frame):
    """Graceful cleanup function."""
    global shm, camera
    logging.info(f"Caught signal {signum}. Shutting down...")
    if camera and camera.isOpened():
        camera.release()
    if shm:
        shm.close()
        try:
            shm.unlink()
        except FileNotFoundError:
            pass
    logging.info("Shutdown complete.")
    sys.exit(0)

def get_camera_source():
    """Reads the desired camera source from the config file."""
    try:
        with open(CONFIG_FILE_PATH, "r") as f:
            return f.read().strip().lower()
    except Exception:
        return 'local'

def run_camera():
    """Main camera loop with robust reconnection."""
    global shm, camera
    
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    try:
        shm = shared_memory.SharedMemory(name=SHM_NAME, create=True, size=SHARED_BUFFER_SIZE)
    except FileExistsError:
        shm = shared_memory.SharedMemory(name=SHM_NAME)

    shared_frame_array = np.ndarray((FRAME_HEIGHT, FRAME_WIDTH, FRAME_CHANNELS), dtype=np.uint8, buffer=shm.buf)
    
    while True:
        try:
            source_type = get_camera_source()
            logging.info(f"Attempting to connect to camera source: {source_type}")

            camera = cv2.VideoCapture(0, cv2.CAP_V4L2) if source_type == 'local' else cv2.VideoCapture(RTSP_STREAM_URL, cv2.CAP_FFMPEG)
            if not camera or not camera.isOpened():
                raise IOError(f"Failed to open camera for source {source_type}.")
            
            logging.info("Camera opened successfully. Starting frame capture.")

            while True:
                ret, frame = camera.read()
                if not ret or frame is None:
                    logging.warning("Failed to grab frame. Breaking to reconnect...")
                    break 
                
                if frame.shape[0] != FRAME_HEIGHT or frame.shape[1] != FRAME_WIDTH:
                    frame = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))
                
                shared_frame_array[:] = frame[:]
                time.sleep(0.01)
        
        except Exception as e:
            logging.error(f"Error in main camera loop: {e}")
        
        finally:
            if camera:
                camera.release()
            logging.info("Camera resource released. Waiting 5s before reconnect...")
            time.sleep(5)

if __name__ == '__main__':
    run_camera()
EOF
echo "Created camera_manager.py"

# --- templates/index.html ---
cat <<'EOF' > $APP_DIR/templates/index.html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hat Detector</title>
    <style>
        body { font-family: Arial, sans-serif; background-color: #f0f0f0; color: #333; display: flex; flex-direction: column; align-items: center; min-height: 100vh; margin: 0; padding: 20px; }
        .container { background-color: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1); text-align: center; margin-bottom: 20px; }
        h1 { color: #0056b3; }
        .video-container { position: relative; width: 1280px; height: 720px; border: 2px solid #ccc; margin-bottom: 10px; overflow: hidden; background-color: #000; }
        #videoFeed { width: 100%; height: 100%; display: block; }
        #overlayCanvas { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; }
        .button-group { margin-bottom: 15px; }
        button { padding: 10px 20px; font-size: 16px; background-color: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; transition: background-color 0.3s ease; margin: 0 5px; }
        button:hover { background-color: #0056b3; }
        button:disabled { background-color: #cccccc; cursor: not-allowed; }
        #analyzeButton { background-color: #28a745; }
        #analyzeButton:hover { background-color: #218838; }
        #statusMessage { margin-top: 15px; font-size: 1.1em; color: #555; min-height: 25px; font-weight: bold; }
        #analysisDetails { background-color: #e9ecef; padding: 15px; border-radius: 8px; text-align: left; width: 1280px; margin-top: 20px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05); }
        #analysisDetails h2 { color: #0056b3; margin-top: 0; border-bottom: 1px solid #cce5ff; padding-bottom: 5px; margin-bottom: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Live Hat Detector</h1>
        <div class="video-container">
            <img id="videoFeed" src="{{ url_for('video_feed') }}" alt="Video Stream">
            <canvas id="overlayCanvas"></canvas>
        </div>
        <div class="button-group">
            <button id="localCamButton">Use Local Camera</button>
            <button id="rtspCamButton">Use RTSP Stream</button>
        </div>
        <button id="analyzeButton">Analyze Frame for Hats</button>
        <div id="statusMessage">Using Local Camera</div>
    </div>

    <div id="analysisDetails">
        <h2>Analysis Results</h2>
        <p><strong>Description:</strong> <span id="descriptionText">No analysis yet.</span></p>
        <p><strong>Tags:</strong> <span id="tagsList">No analysis yet.</span></p>
        <p><strong>All Detected Objects:</strong></p>
        <ul id="objectsList"><li>No analysis yet.</li></ul>
    </div>

    <script>
        const videoFeed = document.getElementById('videoFeed');
        const overlayCanvas = document.getElementById('overlayCanvas');
        const ctx = overlayCanvas.getContext('2d');

        const localCamButton = document.getElementById('localCamButton');
        const rtspCamButton = document.getElementById('rtspCamButton');
        const analyzeButton = document.getElementById('analyzeButton');
        const statusMessageDiv = document.getElementById('statusMessage');

        const descriptionText = document.getElementById('descriptionText');
        const tagsList = document.getElementById('tagsList');
        const objectsList = document.getElementById('objectsList');

        overlayCanvas.width = 1280;
        overlayCanvas.height = 720;

        async function switchCameraSource(source) {
            statusMessageDiv.textContent = `Switching to ${source} stream... Please wait.`;
            localCamButton.disabled = true;
            rtspCamButton.disabled = true;
            analyzeButton.disabled = true;

            try {
                const response = await fetch(`/switch_camera?source=${source}`, { method: 'POST' });
                const data = await response.json();

                if (data.status === 'success' || data.status === 'no_change') {
                    statusMessageDiv.textContent = data.message;
                    videoFeed.src = `/video_feed?t=${new Date().getTime()}`;
                } else {
                    statusMessageDiv.textContent = `Error: ${data.message || 'Could not switch camera.'}`;
                }
            } catch (error) {
                console.error('Error switching camera:', error);
                statusMessageDiv.textContent = 'Error: Could not connect to the server to switch camera.';
            } finally {
                localCamButton.disabled = false;
                rtspCamButton.disabled = false;
                analyzeButton.disabled = false;
            }
        }

        localCamButton.addEventListener('click', () => switchCameraSource('local'));
        rtspCamButton.addEventListener('click', () => switchCameraSource('rtsp'));

        analyzeButton.addEventListener('click', async () => {
            statusMessageDiv.textContent = 'Analysis in progress...';
            analyzeButton.disabled = true;
            ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
            clearAnalysisDetails();

            try {
                const response = await fetch('/analyze_current_frame');
                const data = await response.json();
                if (data.status === 'success') {
                    statusMessageDiv.textContent = `Analysis complete. Found ${data.analysis_data.hat_objects.length} hat(s).`;
                    populateAnalysisDetails(data.analysis_data);
                } else {
                    statusMessageDiv.textContent = `Error: ${data.message || 'Unknown error'}`;
                }
            } catch (error) {
                console.error('Error during analysis:', error);
                statusMessageDiv.textContent = 'Error: Could not connect to analysis service.';
            } finally {
                analyzeButton.disabled = false;
            }
        });

        function populateAnalysisDetails(data) {
             descriptionText.textContent = data.description?.captions?.[0]?.text || 'No description available.';
             tagsList.textContent = data.tags?.map(t => t.name).join(', ') || 'No tags available.';
             if (data.objects && data.objects.length > 0) {
                objectsList.innerHTML = '';
                data.objects.forEach(obj => {
                    const li = document.createElement('li');
                    li.textContent = `${obj.object_property} (${Math.round(obj.confidence * 100)}%)`;
                    objectsList.appendChild(li);
                });
             } else {
                objectsList.innerHTML = '<li>No objects detected.</li>';
             }
        }
        
        function clearAnalysisDetails() {
            descriptionText.textContent = 'No analysis yet.';
            tagsList.textContent = 'No analysis yet.';
            objectsList.innerHTML = '<li>No analysis yet.</li>';
        }
    </script>
</body>
</html>
EOF
echo "Created templates/index.html"

# --- Placeholder Image ---
if [ -f "camera_unavailable.jpg" ]; then
    cp camera_unavailable.jpg $APP_DIR/static/
    echo "Copied camera_unavailable.jpg to static directory."
else
    echo "WARNING: camera_unavailable.jpg not found. The placeholder will not work."
fi

chown -R $APP_USER:$APP_USER $APP_DIR

echo "--- [Step 4/8] Setting up Python virtual environment... ---"
sudo -u $APP_USER python3 -m venv $APP_DIR/venv
sudo -u $APP_USER $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt

echo "--- [Step 5/8] Creating Systemd service files... ---"

# --- camera.service ---
# Note: Variable expansion is used here for $RTSP_URL
cat <<EOF > /etc/systemd/system/camera.service
[Unit]
Description=Hat Detector Camera Service
After=network.target

[Service]
ExecStart=/opt/hat_detector_web_app/venv/bin/python3 /opt/hat_detector_web_app/camera_manager.py
WorkingDirectory=/opt/hat_detector_web_app
Restart=always
User=hat
Group=video
ReadWritePaths=/dev/shm
Environment="RTSP_STREAM_URL=$RTSP_URL"

[Install]
WantedBy=multi-user.target
EOF
echo "Created camera.service"

# --- hat-detector.service ---
# Note: Variable expansion is used here for Azure credentials
cat <<EOF > /etc/systemd/system/hat-detector.service
[Unit]
Description=Hat Detector Web App
After=network.target camera.service
Wants=camera.service

[Service]
Environment="AZURE_VISION_SUBSCRIPTION_KEY=$AZURE_VISION_KEY"
Environment="AZURE_VISION_ENDPOINT=$AZURE_VISION_ENDPOINT"

ExecStart=/opt/hat_detector_web_app/venv/bin/gunicorn --workers 3 --worker-class gevent --bind unix:hat-detector.sock -m 007 app:app
WorkingDirectory=/opt/hat_detector_web_app
StandardOutput=journal
StandardError=journal
Restart=always
User=hat
Group=hat

[Install]
WantedBy=multi-user.target
EOF
echo "Created hat-detector.service"

echo "--- [Step 6/8] Configuring Nginx reverse proxy... ---"
cat <<'EOF' > /etc/nginx/sites-available/hat-detector
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://unix:/opt/hat_detector_web_app/hat-detector.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
EOF
ln -sf /etc/nginx/sites-available/hat-detector /etc/nginx/sites-enabled
rm -f /etc/nginx/sites-enabled/default
aa-complain /etc/apparmor.d/usr.sbin.nginx 2>/dev/null || echo "AppArmor profile for Nginx not found, skipping."
echo "Created Nginx configuration."

echo "--- [Step 7/8] Setting up sudo permissions... ---"
cat <<'EOF' > /etc/sudoers.d/hat-detector-sudo
hat ALL=(ALL) NOPASSWD: /bin/systemctl restart camera.service
EOF
chmod 0440 /etc/sudoers.d/hat-detector-sudo
echo "Sudo permissions configured."

echo "--- [Step 8/8] Activating services... ---"
systemctl daemon-reload
systemctl enable camera.service
systemctl enable hat-detector.service
systemctl restart nginx
systemctl restart camera.service
systemctl restart hat-detector.service

echo ""
echo "============================================================"
echo "✅ Deployment Complete!"
echo "============================================================"
echo "Your application should now be available at your server's IP address."
echo "Example: http://$(hostname -I | awk '{print $1}')"
echo ""
echo "Configuration has been applied from the variables in this script."
echo "============================================================"

exit 0
