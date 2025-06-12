import os
import cv2
import threading
import queue
import time # <--- ADDED THIS LINE
import logging
from flask import Flask, render_template, Response, jsonify
from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from azure.cognitiveservices.vision.computervision.models import VisualFeatureTypes
from msrest.authentication import CognitiveServicesCredentials

# --- Configuration ---
# Azure Computer Vision API credentials
VISION_KEY = os.environ.get("AZURE_VISION_KEY")
VISION_ENDPOINT = os.environ.get("AZURE_VISION_ENDPOINT")

# Set up logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Global State Variables ---
# Queue to hold captured frames from the camera
# A small maxsize is often good for live streams to keep latency low
captured_frame_queue = queue.Queue(maxsize=3)

# Thread-safe variable to hold the latest frame for analysis and display
output_frame = None
output_frame_lock = threading.Lock() # Use a lock for thread-safe access to output_frame

# Camera related
cam = None # OpenCV VideoCapture object
camera_available = False
camera_unavailable_image_bytes = None

# Retry configuration for Azure Computer Vision client
RETRY_CONFIG = {
    'max_retries': 3,
    'backoff_factor': 0.8,
    'max_backoff': 90
}
logger.debug(f"Configuring retry: max_retries={RETRY_CONFIG['max_retries']}, backoff_factor={RETRY_CONFIG['backoff_factor']}, max_backoff={RETRY_CONFIG['max_backoff']}")


# --- Azure Computer Vision Client Initialization ---
# Initialize Azure Computer Vision client
try:
    computervision_client = ComputerVisionClient(
        VISION_ENDPOINT,
        CognitiveServicesCredentials(VISION_KEY)
    )
    logger.info("Computer Vision client initialized.")
except Exception as e:
    logger.error(f"Failed to initialize Computer Vision client: {e}")
    computervision_client = None

# Load placeholder image for when camera is unavailable
try:
    with open("static/camera_unavailable.jpg", "rb") as f:
        camera_unavailable_image_bytes = f.read()
    logger.info("Placeholder image loaded from /opt/hat_detector_web_app_local/static/camera_unavailable.jpg")
except FileNotFoundError:
    logger.error("static/camera_unavailable.jpg not found. Ensure it exists.")
    camera_unavailable_image_bytes = None # Fallback to empty if not found


# --- Flask App Initialization ---
app = Flask(__name__)

# --- Threading Functions ---

def open_webcam(port=0, retries=3):
    """Attempts to open the webcam with retries."""
    global cam, camera_available
    for i in range(retries):
        logger.info(f"Attempting to open webcam at port {port}...")
        cam = cv2.VideoCapture(port)
        if cam.isOpened():
            logger.info(f"Webcam opened successfully at port {port}.")
            camera_available = True
            return True
        logger.warning(f"Failed to open webcam at port {port}, retry {i+1}/{retries}...")
        time.sleep(2 ** i) # Exponential backoff
    logger.error(f"Failed to open webcam at port {port} after {retries} attempts.")
    camera_available = False
    return False

def _capture_loop():
    """Background thread function to capture frames from the webcam."""
    global cam, camera_available, output_frame_lock, output_frame

    logger.info("Background camera capture loop started.")
    if not camera_available:
        if not open_webcam():
            logger.error("Camera not available, capture loop exiting.")
            with output_frame_lock:
                output_frame = None # Ensure output_frame is None if camera fails
            return

    while True:
        logger.debug("_capture_loop: Entered loop iteration.")
        if cam and cam.isOpened():
            logger.debug("_capture_loop: Camera is open, about to call cam.read().")
            ret, frame = cam.read()
            if not ret:
                logger.error("_capture_loop: Failed to read frame from camera. Attempting to re-open.")
                camera_available = False
                cam.release()
                if not open_webcam():
                    logger.error("_capture_loop: Failed to re-open camera, capture loop stopping.")
                    with output_frame_lock:
                        output_frame = None
                    break # Exit loop if camera can't be re-opened
                continue # Try reading again after re-opening
            
            logger.debug("_capture_loop: cam.read() returned ret=True.")
            try:
                # Put the frame into the queue for the generator
                captured_frame_queue.put_nowait(frame)
                logger.debug("_capture_loop: Frame successfully put into queue.")
            except queue.Full:
                logger.debug("_capture_loop: captured_frame_queue is full, dropping frame.")
                # If the queue is full, the consumer is slower than the producer.
                # We drop the oldest frame implicitly by not putting the new one,
                # or you could implement captured_frame_queue.get_nowait() before put.
                # For this application, dropping the oldest is fine to maintain live feed.

            # Also update the global output_frame for analysis
            with output_frame_lock:
                output_frame = frame.copy()
        else:
            if not camera_available:
                if not open_webcam():
                    logger.warning("_capture_loop: Camera still unavailable. Retrying...")
                    with output_frame_lock:
                        output_frame = None
                    time.sleep(2) # Wait before retrying to open camera
                else:
                    logger.info("_capture_loop: Camera re-opened successfully.")
            else:
                logger.error("_capture_loop: Camera object is unexpectedly closed despite being marked available. Re-opening.")
                cam.release() # Ensure it's truly released
                if not open_webcam():
                    logger.error("_capture_loop: Failed to re-open camera, capture loop stopping.")
                    with output_frame_lock:
                        output_frame = None
                    break # Exit loop if camera can't be re-opened


def analyze_frame(frame_to_analyze):
    """Function to send a frame to Azure Computer Vision for analysis."""
    logger.info("analyze_frame: Received request to analyze frame.")
    if computervision_client is None:
        logger.error("analyze_frame: Computer Vision client is not initialized.")
        return {"error": "Computer Vision service unavailable."}

    if frame_to_analyze is None:
        logger.warning("analyze_frame: output_frame is None at the start of analysis request. Camera may not be streaming.")
        # Return a message indicating camera not streaming rather than error
        return {"result": "No live webcam frame available. Camera may not be streaming."}

    try:
        # Convert the OpenCV frame (numpy array) to bytes for API call
        is_success, im_buf_arr = cv2.imencode(".jpg", frame_to_analyze)
        if not is_success:
            logger.error("analyze_frame: Failed to encode frame to JPG.")
            return {"error": "Failed to encode frame for analysis."}
        byte_stream = im_buf_arr.tobytes()

        # Analyze the image for tags (including 'hat')
        logger.info("analyze_frame: Calling Azure CV API...")
        image_analysis = computervision_client.analyze_image_in_stream(
            byte_stream, visual_features=[VisualFeatureTypes.tags]
        )
        logger.info("analyze_frame: Azure CV API call complete.")

        hat_detected = False
        tags = []
        for tag in image_analysis.tags:
            tags.append(tag.name)
            if "hat" in tag.name.lower(): # Check if 'hat' is in the tag name
                hat_detected = True

        result = {
            "hat_detected": hat_detected,
            "tags": tags,
            "message": "Hat detected!" if hat_detected else "No hat detected."
        }
        logger.info(f"analyze_frame: Analysis result: {result}")
        return result

    except Exception as e:
        logger.error(f"analyze_frame: Error during analysis: {e}", exc_info=True)
        return {"error": f"An error occurred during analysis: {e}"}


# --- Flask Routes ---

@app.route('/')
def index():
    """Video streaming home page."""
    return render_template('index.html')

def generate_frames():
    """Video streaming generator function."""
    global output_frame, camera_unavailable_image_bytes, output_frame_lock

    while True:
        try:
            # Get a frame from the queue, with a timeout
            # If the queue is empty, a queue.Empty exception is raised after timeout
            frame = captured_frame_queue.get(timeout=10) # <--- TIMEOUT INCREASED TO 10
            
            if frame is not None:
                # Update global output_frame with the latest frame for display
                with output_frame_lock:
                    output_frame = frame.copy()

                ret, buffer = cv2.imencode('.jpg', frame)
                if not ret:
                    logger.error("generate_frames: Failed to encode frame to JPEG for streaming.")
                    # Optionally, yield placeholder if encoding fails
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + bytearray(camera_unavailable_image_bytes) + b'\r\n')
                    time.sleep(1) # Prevent tight loop on encoding failure
                    continue

                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + bytearray(buffer) + b'\r\n')
                
                time.sleep(0.05) # <--- ADDED THIS LINE (yield approx 20 frames/sec)
            else:
                # This path should ideally not be hit if queue.get raises Empty
                # but included for robustness if a None frame somehow enters the queue
                logger.warning("generate_frames: Received None frame from queue. Sending placeholder.")
                with output_frame_lock:
                    output_frame = None
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + bytearray(camera_unavailable_image_bytes) + b'\r\n')
                time.sleep(1) # Prevent tight loop

        except queue.Empty:
            # If no frame is received within the timeout, send the placeholder image
            logger.warning("generate_frames: No frame received from capture thread within timeout. Sending placeholder.")
            with output_frame_lock:
                output_frame = None # Ensure output_frame is None if stream stalls
            if camera_unavailable_image_bytes:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + bytearray(camera_unavailable_image_bytes) + b'\r\n')
            time.sleep(1) # Wait before trying again to prevent CPU spin

        except Exception as e: # <--- ADDED THIS BLOCK
            logger.error(f"generate_frames: An unexpected error occurred: {e}", exc_info=True)
            with output_frame_lock:
                output_frame = None
            if camera_unavailable_image_bytes:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + bytearray(camera_unavailable_image_bytes) + b'\r\n')
            time.sleep(1) # Wait to prevent tight loop on error


@app.route('/video_feed')
def video_feed():
    """Video streaming route. Supplies the live video feed."""
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/analyze_current_frame')
def analyze_current_frame():
    """API endpoint to analyze the current frame."""
    global output_frame, output_frame_lock
    
    frame_to_analyze = None
    with output_frame_lock:
        if output_frame is not None:
            frame_to_analyze = output_frame.copy() # Get a copy for analysis

    return jsonify(analyze_frame(frame_to_analyze))


# --- Startup ---
if __name__ == '__main__':
    # Start the background camera capture thread
    camera_thread = threading.Thread(target=_capture_loop, daemon=True)
    camera_thread.start()
    logger.info("Background camera capture thread started.")

    # In local development, you might run with app.run() directly.
    # For Gunicorn, this __name__ == '__main__' block is skipped.
    # When running with Gunicorn, Gunicorn manages the processes and threads.
    # The _capture_loop thread is started within each worker process.
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True) # Usually run with Gunicorn
