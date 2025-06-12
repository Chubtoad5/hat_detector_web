import gevent.monkey
# It's crucial to patch_all() early, but keep in mind that
# multiprocessing bypasses gevent's patching for inter-process communication
gevent.monkey.patch_all()

import os
import cv2
import numpy as np # Required for converting shared memory to NumPy array
import multiprocessing
import queue
import time
import logging
import io
from flask import Flask, render_template, Response, jsonify, request
from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from msrest.authentication import CognitiveServicesCredentials
import json
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Configure logging
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(processName)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Configuration ---
# Azure Cognitive Services (Vision API) configuration
COMPUTER_VISION_SUBSCRIPTION_KEY = os.environ.get("AZURE_VISION_SUBSCRIPTION_KEY")
COMPUTER_VISION_ENDPOINT = os.environ.get("AZURE_VISION_ENDPOINT")

# If keys are not set as environment variables, use placeholders for local testing
if not COMPUTER_VISION_SUBSCRIPTION_KEY:
    logger.warning("AZURE_VISION_SUBSCRIPTION_KEY environment variable not set. Using placeholder.")
    COMPUTER_VISION_SUBSCRIPTION_KEY = "YOUR_AZURE_VISION_SUBSCRIPTION_KEY" # Replace with your actual key
if not COMPUTER_VISION_ENDPOINT:
    logger.warning("AZURE_VISION_ENDPOINT environment variable not set. Using placeholder.")
    COMPUTER_VISION_ENDPOINT = "https://your-resource-name.cognitiveservices.azure.com/" # Replace with your actual endpoint

# API Retry Configuration (for Azure Cognitive Services)
RETRY_CONFIG = {
    'max_retries': 5,
    'backoff_factor': 2,
    'max_backoff': 60
}
logger.debug(f"Configuring retry: max_retries={RETRY_CONFIG['max_retries']}, backoff_factor={RETRY_CONFIG['backoff_factor']}, max_backoff={RETRY_CONFIG['max_backoff']}")

# --- Shared Memory Configuration ---
# Define fixed frame dimensions for the shared buffer
FRAME_HEIGHT = 480
FRAME_WIDTH = 640
FRAME_CHANNELS = 3 # BGR color
# Calculate total size in bytes (each pixel is 3 bytes for BGR)
SHARED_BUFFER_SIZE = FRAME_HEIGHT * FRAME_WIDTH * FRAME_CHANNELS

# Global variables for camera feed and analysis
# Use multiprocessing.Array for shared frame data
shared_frame_buffer = multiprocessing.Array('B', SHARED_BUFFER_SIZE) # 'B' for unsigned char (byte), size in bytes
shared_frame_lock = multiprocessing.Lock() # Lock to protect shared buffer access

camera_process = None # To hold the multiprocessing.Process object
camera_available_in_mp_process = multiprocessing.Value('b', False) # Shared boolean for camera status

camera_unavailable_image_bytes = None # To store the placeholder image bytes

# --- Azure Computer Vision Client Initialization ---
computervision_client = None
if COMPUTER_VISION_SUBSCRIPTION_KEY and COMPUTER_VISION_ENDPOINT and \
   COMPUTER_VISION_SUBSCRIPTION_KEY != "YOUR_AZURE_VISION_SUBSCRIPTION_KEY" and \
   COMPUTER_VISION_ENDPOINT != "https://your-resource-name.cognitiveservices.azure.com/":
    try:
        computervision_client = ComputerVisionClient(
            endpoint=COMPUTER_VISION_ENDPOINT,
            credentials=CognitiveServicesCredentials(COMPUTER_VISION_SUBSCRIPTION_KEY)
        )
        logger.info("Computer Vision client initialized.")
    except Exception as e:
        logger.error(f"Error initializing Computer Vision client: {e}")
else:
    logger.warning("Computer Vision client not initialized. Azure credentials are missing or placeholder.")

# --- Load Placeholder Image ---
try:
    with open("static/camera_unavailable.jpg", "rb") as f:
        camera_unavailable_image_bytes = f.read()
    logger.info("Placeholder image loaded from static/camera_unavailable.jpg")
except FileNotFoundError:
    logger.error("Placeholder image (static/camera_unavailable.jpg) not found!")
    camera_unavailable_image_bytes = None

# --- Camera Process Worker Function ---
# This function will run in a separate OS process
def camera_process_worker(shared_buffer, shared_lock, camera_status_shared):
    """
    Worker function for the camera process.
    Continuously captures frames and writes them into the shared buffer.
    """
    logger.info("Camera process worker started.")
    camera = None
    try:
        logger.info("Attempting to open webcam at port 0 in camera process...")
        camera = cv2.VideoCapture(0) # Try to open the default webcam
        if not camera.isOpened():
            logger.error("Camera process: FATAL - Failed to open webcam at port 0.")
            camera_status_shared.value = False
            return # Exit process if camera cannot be opened

        # Try to set resolution (optional, but good practice if you expect a specific size)
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

        camera_status_shared.value = True
        logger.info(f"Camera process: Webcam opened successfully at port 0. Expected resolution: {FRAME_WIDTH}x{FRAME_HEIGHT}")

        while camera_status_shared.value: # Loop as long as camera is considered available
            ret, frame = camera.read() # Read a frame from the camera

            if not ret or frame is None:
                logger.warning(f"Camera process: Failed to grab frame (ret={ret}, frame is None={frame is None}). Attempting to re-open camera.")
                if camera:
                    camera.release()
                camera = cv2.VideoCapture(0) # Try re-opening
                if not camera.isOpened():
                    logger.error("Camera process: Failed to re-open webcam. Setting status to unavailable and exiting.")
                    camera_status_shared.value = False
                    break # Exit loop if re-opening fails
                logger.info("Camera process: Webcam re-opened successfully after grab failure.")
                time.sleep(0.5) # Give camera a moment
                continue # Skip current iteration to get a new frame

            # Ensure the frame matches the expected dimensions
            if frame.shape[0] != FRAME_HEIGHT or frame.shape[1] != FRAME_WIDTH or frame.shape[2] != FRAME_CHANNELS:
                logger.warning(f"Camera process: Captured frame has unexpected shape {frame.shape}. Resizing to {FRAME_WIDTH}x{FRAME_HEIGHT}x{FRAME_CHANNELS}.")
                frame = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT)) # Resize if needed
                if frame.shape[2] != FRAME_CHANNELS: # Handle grayscale to color if necessary
                    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR) # Convert to BGR if it's grayscale

            # Acquire lock before writing to shared memory
            with shared_lock:
                # Copy the frame data (NumPy array) into the shared buffer
                # Ensure it's contiguous C-style array for tobytes()
                if not frame.flags['C_CONTIGUOUS']:
                    frame = np.ascontiguousarray(frame)

                # Directly write bytes to the shared_buffer
                # This is efficient, but requires exact size match
                try:
                    shared_buffer[:len(frame.tobytes())] = frame.tobytes()
                    # logger.debug("Camera process: Frame successfully written to shared buffer.") # Very verbose
                except ValueError as e:
                    logger.error(f"Camera process: Error writing to shared buffer (size mismatch?): {e}", exc_info=True)
                    # This could happen if frame.tobytes() length doesn't match buffer size
                    # Fallback if there's a size mismatch issue
                    # for i, byte_val in enumerate(frame.tobytes()):
                    #    shared_buffer[i] = byte_val

            # A short sleep to yield CPU, crucial for multiprocessing worker not to spin excessively
            time.sleep(0.02) # Aim for ~30 FPS

    except Exception as e:
        logger.error(f"Camera process: An unexpected error occurred in worker loop: {e}", exc_info=True)
        camera_status_shared.value = False
    finally:
        if camera:
            logger.info("Camera process: Releasing camera resource.")
            camera.release()
        logger.info("Camera process: Exiting.")


# --- Process Initialization (Main Flask App) ---
# This block runs when the Flask app (and thus Gunicorn worker) is booted
def start_camera_process():
    global camera_process, shared_frame_buffer, shared_frame_lock, camera_available_in_mp_process
    if camera_process is None or not camera_process.is_alive():
        logger.info("Main process: Starting camera capture process...")
        # Pass the shared buffer, lock, and status to the camera worker function
        camera_process = multiprocessing.Process(
            target=camera_process_worker,
            args=(shared_frame_buffer, shared_frame_lock, camera_available_in_mp_process),
            daemon=True # Make it a daemon so it terminates with the parent process
        )
        camera_process.start()
        logger.info(f"Main process: Camera process started with PID: {camera_process.pid}")
    else:
        logger.info("Main process: Camera process already running.")

# Call this function when the Flask app starts
start_camera_process()


# --- Helper Function for Computer Vision Analysis ---
@retry(stop=stop_after_attempt(RETRY_CONFIG['max_retries']),
        wait=wait_exponential(multiplier=RETRY_CONFIG['backoff_factor'], max=RETRY_CONFIG['max_backoff']),
        retry=retry_if_exception_type(requests.exceptions.RequestException))
def analyze_frame_core(frame_bytes):
    """Core function to send an image to Azure Computer Vision API for analysis with retries."""
    logger.debug("analyze_frame_core: Starting frame encoding.")
    if not computervision_client:
        logger.error("analyze_frame_core: Computer Vision client not initialized.")
        raise ValueError("Computer Vision client not configured.")

    try:
        analysis = computervision_client.analyze_image_in_stream(
            image=io.BytesIO(frame_bytes), # Use io.BytesIO for stream analysis
            visual_features=["Objects", "Tags", "Description"]
        )
        logger.debug("analyze_frame_core: Azure CV API call complete.")
        return analysis

    except Exception as e:
        logger.error(f"analyze_frame_core: Error during Azure CV API call: {e}", exc_info=True)
        raise # Re-raise to allow tenacity to handle retries


# --- Thread Pool for Asynchronous Analysis (within Flask app process) ---
analysis_executor = None
try:
    from concurrent.futures import ThreadPoolExecutor
    analysis_executor = ThreadPoolExecutor(max_workers=1) # One worker for analysis
    logger.info("ThreadPoolExecutor for analysis initialized.")
except ImportError:
    logger.warning("ThreadPoolExecutor not available. Analysis will run synchronously.")

current_analysis_task_id = None # To track the latest analysis request
analysis_results_cache = {} # Cache for analysis results by ID

# --- Flask Routes ---

@app.route('/test')
def test_route():
    logger.info("TEST: Test route hit!")
    return "Hello from Test Route!"

@app.route('/')
def index():
    """Video streaming home page."""
    logger.info("INDEX: Index route hit!")
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    """Video streaming route. It continually yields camera frames."""
    logger.info("VIDEO_FEED: Video feed requested.")
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

def generate_frames():
    """Video streaming generator function (reads from shared memory)."""
    # This line is crucial for accessing global variables
    global shared_frame_buffer, shared_frame_lock, camera_available_in_mp_process, camera_unavailable_image_bytes

    while True:
        frame = None
        # Check if camera is available from the camera process's perspective
        if not camera_available_in_mp_process.value:
            logger.warning("generate_frames: Camera process reports camera not available. Sending placeholder.")
            if camera_unavailable_image_bytes:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + bytearray(camera_unavailable_image_bytes) + b'\r\n')
            else:
                logger.error("generate_frames: Placeholder image not loaded. Cannot stream.")
                yield b''
            time.sleep(1) # Wait a bit before trying again
            continue

        # Acquire lock to read from shared buffer
        with shared_frame_lock: # Using shared_frame_lock here
            # Create a NumPy array view of the shared memory, then copy it
            # This avoids potential issues if the camera process writes while we read
            try:
                # The shared_buffer is an array of bytes. We convert it to a numpy array.
                # np.frombuffer is efficient for this.
                frame_bytes = shared_frame_buffer[:] # Get a copy of bytes from shared memory
                frame = np.frombuffer(bytearray(frame_bytes), dtype=np.uint8).reshape((FRAME_HEIGHT, FRAME_WIDTH, FRAME_CHANNELS))
                # logger.debug("generate_frames: Successfully retrieved frame from shared buffer.") # Very verbose
            except ValueError as e:
                logger.error(f"generate_frames: Error creating numpy array from shared buffer: {e}", exc_info=True)
                frame = None # Indicate failure to get frame
            except Exception as e:
                logger.error(f"generate_frames: Unexpected error during frame retrieval: {e}", exc_info=True)
                frame = None

        if frame is None:
            logger.warning("generate_frames: No valid frame obtained from shared memory. Sending placeholder.")
            if camera_unavailable_image_bytes:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + bytearray(camera_unavailable_image_bytes) + b'\r\n')
            else:
                yield b''
            time.sleep(1)
            continue # Try next frame

        # Encode the NumPy frame to JPEG bytes
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            logger.error("generate_frames: Failed to encode frame to JPEG. Sending placeholder.")
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + bytearray(camera_unavailable_image_bytes) + b'\r\n')
            time.sleep(0.1)
            continue

        # If successful, yield the JPEG bytes
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + bytearray(jpeg.tobytes()) + b'\r\n')

        time.sleep(0.033) # Aim for roughly 30 FPS video stream

@app.route('/analyze_current_frame')
def analyze_current_frame():
    """API endpoint to trigger analysis of the current frame from shared memory."""
    # This line is crucial for accessing global variables
    global shared_frame_buffer, shared_frame_lock, camera_available_in_mp_process, analysis_executor, current_analysis_task_id, analysis_results_cache

    if not camera_available_in_mp_process.value:
        logger.warning("/analyze_current_frame: Camera not available in separate process. Cannot analyze.")
        return jsonify({"status": "error", "message": "Camera is not available. Cannot analyze live frame."}), 400

    if not computervision_client:
        logger.error("/analyze_current_frame: Computer Vision client not initialized. Cannot analyze.")
        return jsonify({"status": "error", "message": "Azure Computer Vision client not configured. Cannot analyze."}), 500

    frame_to_analyze = None
    # Acquire lock to read from shared buffer
    with shared_frame_lock: # Using shared_frame_lock here
        try:
            frame_bytes = shared_frame_buffer[:] # Get a copy of bytes from shared memory
            frame_to_analyze = np.frombuffer(bytearray(frame_bytes), dtype=np.uint8).reshape((FRAME_HEIGHT, FRAME_WIDTH, FRAME_CHANNELS))
            logger.debug("/analyze_current_frame: Got frame from shared buffer for analysis.")
        except ValueError as e:
            logger.error(f"/analyze_current_frame: Error creating numpy array from shared buffer: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"/analyze_current_frame: Unexpected error during frame retrieval: {e}", exc_info=True)

    if frame_to_analyze is None:
        logger.warning("/analyze_current_frame: No valid frame obtained from shared memory for analysis.")
        return jsonify({"status": "error", "message": "Failed to obtain a live frame from camera for analysis. Please try again."}), 408 # Request Timeout


    # Encode frame to JPEG bytes for API submission
    ret, jpeg_bytes = cv2.imencode('.jpg', frame_to_analyze)
    if not ret:
        logger.error("/analyze_current_frame: Failed to encode frame to JPEG for analysis.")
        return jsonify({"status": "error", "message": "Failed to process image for analysis."}), 500

    # Generate a unique ID for this analysis request
    request_id = os.urandom(8).hex()
    current_analysis_task_id = request_id # Track the latest task (optional)
    analysis_results_cache[request_id] = {"status": "pending", "message": "Analysis started."}

    logger.info(f"Analysis request {request_id}: Submitting frame to analysis executor.")

    def run_analysis_task(rid, frame_b):
        try:
            result = analyze_frame_core(frame_b)
            # Convert SDK objects to serializable dicts
            formatted_result = {
                "description": result.description.as_dict() if result.description else {},
                "tags": [tag.as_dict() for tag in result.tags] if result.tags else [],
                "objects": [obj.as_dict() for obj in result.objects] if result.objects else []
            }
            analysis_results_cache[rid] = {"status": "completed", "result": formatted_result}
            logger.info(f"Analysis request {rid}: Completed successfully.")
        except Exception as ex:
            logger.error(f"Analysis request {rid}: Failed with error: {ex}", exc_info=True)
            analysis_results_cache[rid] = {"status": "failed", "message": str(ex)}

    if analysis_executor:
        # Submit the analysis to the thread pool
        analysis_executor.submit(run_analysis_task, request_id, jpeg_bytes.tobytes())
    else:
        # Fallback to synchronous analysis if ThreadPoolExecutor is not available
        logger.warning(f"Analysis request {request_id}: Running synchronously (ThreadPoolExecutor not available).")
        run_analysis_task(request_id, jpeg_bytes.tobytes())

    return jsonify({"status": "Analysis started", "request_id": request_id})


@app.route('/get_analysis_status/<task_id>')
def get_analysis_status(task_id):
    """API endpoint to get the status and results of an analysis task."""
    logger.debug(f"Received request for analysis status for task ID: {task_id}")
    result = analysis_results_cache.get(task_id)
    if result:
        # Return the cached result and remove it if it's a final state
        if result['status'] in ['completed', 'failed']:
            pass # Keep for debugging for now
        return jsonify(result)
    else:
        logger.debug(f"Analysis task ID {task_id} not found in cache.")
        return jsonify({"status": "not_found", "message": "Analysis request not found or expired."})


# --- Error Handlers ---
@app.errorhandler(404)
def page_not_found(e):
    logger.warning(f"404 Not Found: {request.path}")
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    logger.error(f"500 Internal Server Error: {e}", exc_info=True)
    return render_template('500.html'), 500

# --- Cleanup on Shutdown (crucial for multiprocessing) ---
import atexit
@atexit.register
def cleanup_on_exit():
    global camera_process, analysis_executor
    logger.info("Main process: Starting cleanup on exit.")

    if camera_process and camera_process.is_alive():
        logger.info(f"Main process: Terminating camera process (PID: {camera_process.pid}).")
        # Set shared flag to false to signal camera process to exit gracefully
        camera_available_in_mp_process.value = False
        camera_process.join(timeout=5) # Give it 5 seconds to exit
        if camera_process.is_alive():
            logger.warning("Main process: Camera process did not terminate gracefully. Forcing kill.")
            camera_process.terminate()
            camera_process.join(timeout=1) # Wait a bit more
    else:
        logger.info("Main process: Camera process not running or already terminated.")

    if analysis_executor:
        logger.info("Main process: Shutting down analysis ThreadPoolExecutor.")
        analysis_executor.shutdown(wait=True, cancel_futures=True)

    logger.info("Main process: Application shutdown complete.")


if __name__ == '__main__':
    logger.info("Running Flask app in development mode (via __name__ == '__main__').")
    start_camera_process()
    app.run(host='0.0.0.0', port=8000, debug=False, use_reloader=False)
