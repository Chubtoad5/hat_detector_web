import gevent.monkey
gevent.monkey.patch_all()

import os
import cv2
import numpy as np
import multiprocessing
import queue
import time
import logging
import io
from flask import Flask, render_template, Response, jsonify, request
from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from msrest.authentication import CognitiveServicesCredentials
from concurrent.futures import ThreadPoolExecutor

# =============================================================================
# CONFIGURATION
# =============================================================================

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(processName)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Application Configuration ---
COMPUTER_VISION_SUBSCRIPTION_KEY = os.environ.get("AZURE_VISION_SUBSCRIPTION_KEY")
COMPUTER_VISION_ENDPOINT = os.environ.get("AZURE_VISION_ENDPOINT")
RTSP_STREAM_URL = os.environ.get("RTSP_STREAM_URL", "rtsp://192.168.29.26/axis-media/media.amp")
os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = 'rtsp_transport;tcp'

# --- Frame & Shared Memory Configuration ---
FRAME_HEIGHT = 720
FRAME_WIDTH = 1280
FRAME_CHANNELS = 3
SHARED_BUFFER_SIZE = FRAME_HEIGHT * FRAME_WIDTH * FRAME_CHANNELS
SHARED_STATE_BUFFER_SIZE = 10 # For storing 'local' or 'rtsp'

# =============================================================================
# INITIALIZATION (GLOBAL SCOPE)
# =============================================================================

# --- Shared Memory Objects ---
# These are created in the main process and inherited by child processes.
shared_frame_buffer = multiprocessing.Array('B', SHARED_BUFFER_SIZE)
shared_frame_lock = multiprocessing.Lock()
camera_process = None
camera_available_in_mp_process = multiprocessing.Value('b', False)
# Using a simple shared array for state is more robust than a Manager.
current_camera_source = multiprocessing.Array('c', b'local'.ljust(SHARED_STATE_BUFFER_SIZE, b'\0'))

# --- Helper Objects ---
camera_unavailable_image_bytes = None
computervision_client = None
analysis_executor = ThreadPoolExecutor(max_workers=1)

# --- Load Placeholder Image ---
try:
    with open("static/camera_unavailable.jpg", "rb") as f:
        camera_unavailable_image_bytes = f.read()
    logger.info("Placeholder image loaded.")
except Exception as e:
    logger.error(f"Could not load placeholder image: {e}")

# --- Initialize Azure Client ---
if COMPUTER_VISION_SUBSCRIPTION_KEY and COMPUTER_VISION_ENDPOINT:
    try:
        computervision_client = ComputerVisionClient(
            endpoint=COMPUTER_VISION_ENDPOINT,
            credentials=CognitiveServicesCredentials(COMPUTER_VISION_SUBSCRIPTION_KEY)
        )
        logger.info("Computer Vision client initialized.")
    except Exception as e:
        logger.error(f"Could not initialize Computer Vision client: {e}")
else:
    logger.warning("Azure credentials not found. Analysis will be disabled.")

# =============================================================================
# CAMERA PROCESS WORKER
# =============================================================================

def camera_process_worker(source_type, shared_buffer, shared_lock, camera_status_shared):
    logger.info(f"Camera process started for source type: {source_type}")
    camera = None
    try:
        if source_type == 'local':
            logger.info("Attempting to open local webcam (index 0)...")
            camera = cv2.VideoCapture(0)
            camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        elif source_type == 'rtsp':
            logger.info(f"Attempting to open RTSP stream: {RTSP_STREAM_URL}")
            camera = cv2.VideoCapture(RTSP_STREAM_URL, cv2.CAP_FFMPEG)
        else:
            logger.error(f"Unknown source type: {source_type}")
            camera_status_shared.value = False
            return

        if not camera.isOpened():
            logger.error(f"FATAL: Failed to open camera for source {source_type}.")
            camera_status_shared.value = False
            return

        camera_status_shared.value = True
        logger.info(f"Camera for source '{source_type}' opened successfully.")

        while camera_status_shared.value:
            ret, frame = camera.read()
            if not ret or frame is None:
                logger.warning(f"Failed to grab frame from source '{source_type}'. Reconnecting in 5s...")
                if camera: camera.release()
                time.sleep(5)
                if source_type == 'local': camera = cv2.VideoCapture(0)
                else: camera = cv2.VideoCapture(RTSP_STREAM_URL, cv2.CAP_FFMPEG)
                continue

            if frame.shape[0] != FRAME_HEIGHT or frame.shape[1] != FRAME_WIDTH:
                frame = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))

            with shared_lock:
                frame_bytes = frame.tobytes()
                shared_buffer[:len(frame_bytes)] = frame_bytes
            
            time.sleep(0.001)

    except Exception as e:
        logger.error(f"Camera process crashed: {e}", exc_info=True)
    finally:
        camera_status_shared.value = False
        if camera: camera.release()
        logger.info(f"Camera process for source '{source_type}' has exited.")

# =============================================================================
# PROCESS MANAGEMENT
# =============================================================================

def stop_camera_process():
    global camera_process
    if camera_process and camera_process.is_alive():
        logger.info(f"Stopping camera process (PID: {camera_process.pid}).")
        camera_available_in_mp_process.value = False
        camera_process.join(timeout=3)
        if camera_process.is_alive():
            logger.warning("Camera process did not stop gracefully. Terminating.")
            camera_process.terminate()
            camera_process.join(timeout=1)
        logger.info("Camera process stopped.")
    camera_process = None

def start_camera_process(source_type='local'):
    global camera_process
    stop_camera_process()
    
    logger.info(f"Starting new camera process for source: {source_type}")
    with current_camera_source.get_lock():
        current_camera_source.value = source_type.encode().ljust(SHARED_STATE_BUFFER_SIZE, b'\0')

    camera_process = multiprocessing.Process(
        target=camera_process_worker,
        args=(source_type, shared_frame_buffer, shared_frame_lock, camera_available_in_mp_process),
        daemon=True
    )
    camera_process.start()
    logger.info(f"New camera process started with PID: {camera_process.pid}")

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    def generate_frames():
        while True:
            if not camera_available_in_mp_process.value:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + camera_unavailable_image_bytes + b'\r\n')
                time.sleep(1)
                continue
            
            with shared_frame_lock:
                frame_bytes = shared_frame_buffer[:]
            
            frame = np.frombuffer(bytearray(frame_bytes), dtype=np.uint8).reshape((FRAME_HEIGHT, FRAME_WIDTH, FRAME_CHANNELS))
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret: continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(0.033)
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/switch_camera', methods=['POST'])
def switch_camera():
    new_source = request.args.get('source', 'local').lower()
    if new_source not in ['local', 'rtsp']:
        return jsonify({"status": "error", "message": "Invalid source type"}), 400

    with current_camera_source.get_lock():
        current_source_str = current_camera_source.value.decode().strip('\0')
    
    if new_source == current_source_str:
        return jsonify({"status": "no_change", "message": f"Camera source is already {new_source}"})

    logger.info(f"Switching camera source from '{current_source_str}' to '{new_source}'")
    try:
        start_camera_process(source_type=new_source)
        time.sleep(3) # Give camera time to initialize before responding
        return jsonify({"status": "success", "message": f"Switched to {new_source} camera."})
    except Exception as e:
        logger.error(f"Failed to switch camera: {e}")
        return jsonify({"status": "error", "message": "Failed to switch camera source"}), 500

@app.route('/analyze_current_frame')
def analyze_current_frame():
    if not camera_available_in_mp_process.value:
        return jsonify({"status": "error", "message": "Camera is not available"}), 400
    if not computervision_client:
        return jsonify({"status": "error", "message": "Azure client not configured"}), 500

    with shared_frame_lock:
        frame_bytes = shared_frame_buffer[:]
    
    frame_to_analyze = np.frombuffer(bytearray(frame_bytes), dtype=np.uint8).reshape((FRAME_HEIGHT, FRAME_WIDTH, FRAME_CHANNELS))
    ret, jpeg_bytes = cv2.imencode('.jpg', frame_to_analyze)
    if not ret:
        return jsonify({"status": "error", "message": "Failed to encode frame"}), 500

    def run_analysis_task(frame_b, result_queue):
        try:
            analysis = computervision_client.analyze_image_in_stream(
                image=io.BytesIO(frame_b),
                visual_features=["Objects", "Tags", "Description"]
            )
            data = {"description": analysis.description.as_dict() if analysis.description else {},
                    "tags": [t.as_dict() for t in analysis.tags] if analysis.tags else [],
                    "objects": [], "hat_objects": []}
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
    try:
        result = result_queue.get(timeout=15)
        if result['status'] == 'completed':
            return jsonify({"status": "success", "analysis_data": result['analysis_data']})
        else:
            return jsonify({"status": "error", "message": result['message']}), 500
    except queue.Empty:
        return jsonify({"status": "error", "message": "Analysis processing timed out."}), 504

# =============================================================================
# APPLICATION STARTUP
# =============================================================================
# When running with Gunicorn, use the --preload flag. This will cause this
# code to run once in the master process before workers are forked.
start_camera_process(source_type='local')
