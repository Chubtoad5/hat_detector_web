import cv2
import os
import time
from flask import Flask, render_template, Response, jsonify, request
from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from azure.cognitiveservices.vision.computervision.models import VisualFeatureTypes
from msrest.authentication import CognitiveServicesCredentials
from io import BytesIO
import threading
import queue
import logging

app = Flask(__name__)

# --- Configure logging ---
# This ensures logs go to the systemd journal for easy monitoring
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Azure Computer Vision Configuration ---
VISION_ENDPOINT = os.environ.get("VISION_ENDPOINT")
VISION_KEY = os.environ.get("VISION_KEY")

if not VISION_ENDPOINT or not VISION_KEY:
    logger.warning("VISION_ENDPOINT or VISION_KEY environment variables are not set.")
    logger.warning("Please ensure they are configured correctly in the systemd service file.")
    # Assign placeholders to avoid immediate crash, but analysis will fail.
    VISION_ENDPOINT = VISION_ENDPOINT if VISION_ENDPOINT else "YOUR_AZURE_VISION_ENDPOINT_PLACEHOLDER"
    VISION_KEY = VISION_KEY if VISION_KEY else "YOUR_AZURE_VISION_KEY_PLACEHOLDER"

# Authenticate with Azure Computer Vision
computervision_client = None
try:
    if VISION_ENDPOINT and VISION_ENDPOINT != "YOUR_AZURE_VISION_ENDPOINT_PLACEHOLDER":
        computervision_client = ComputerVisionClient(
            VISION_ENDPOINT, CognitiveServicesCredentials(VISION_KEY)
        )
        logger.info("Computer Vision client initialized.")
    else:
        logger.error("Skipping Computer Vision client initialization: Endpoint/Key not set or are placeholders.")
except Exception as e:
    logger.error(f"Error initializing Computer Vision client: {e}")
    logger.error("Please check your VISION_ENDPOINT and VISION_KEY.")
    computervision_client = None

# --- Webcam Configuration ---
CAMERA_PORT = 0 # Typically /dev/video0 on Linux

# --- Global variables for camera feed and analysis results ---
camera = None # Store the OpenCV camera object
output_frame = None # Store the last captured frame (or placeholder) for analysis
lock = threading.Lock() # For thread-safe access to output_frame

# --- Global variables for analysis results ---
current_hat_status = "Not analyzed yet."
detected_objects_for_display = []
last_analysis_triggered_time = 0

# --- Threading for AI Analysis ---
# Queue to send frames to the analysis thread
analysis_queue = queue.Queue(maxsize=1) # Max size 1 to always process the latest frame
# Queue to receive results from the analysis thread
analysis_result_queue = queue.Queue(maxsize=1) # Max size 1 for the latest result

# --- Path to the static placeholder image ---
CAMERA_UNAVAILABLE_IMAGE_PATH = os.path.join(app.root_path, 'static', 'camera_unavailable.jpg')
camera_unavailable_image_bytes = None

# Pre-load the placeholder image bytes to avoid re-reading on every frame
try:
    with open(CAMERA_UNAVAILABLE_IMAGE_PATH, 'rb') as f:
        camera_unavailable_image_bytes = f.read()
    logger.info(f"Placeholder image loaded from {CAMERA_UNAVAILABLE_IMAGE_PATH}")
except FileNotFoundError:
    logger.error(f"WARNING: Placeholder image not found at {CAMERA_UNAVAILABLE_IMAGE_PATH}. Please create it.")
    camera_unavailable_image_bytes = None
except Exception as e:
    logger.error(f"Error loading placeholder image from {CAMERA_UNAVAILABLE_IMAGE_PATH}: {e}")
    camera_unavailable_image_bytes = None

def get_camera():
    """Initializes or returns the camera object, handling re-attempts."""
    global camera
    # If camera is already open, return it
    if camera is not None and camera.isOpened():
        return camera

    # Try to open/re-open camera
    logger.info(f"Attempting to open webcam at port {CAMERA_PORT}...")
    try:
        # Explicitly use CAP_V4L2 backend for Linux stability
        camera = cv2.VideoCapture(CAMERA_PORT, cv2.CAP_V4L2)
        # Set resolution (common standard, can be adjusted)
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        camera.set(cv2.CAP_PROP_FPS, 30) # Attempt to set FPS

        if not camera.isOpened():
            logger.error(f"Error: Could not open webcam at port {CAMERA_PORT}. It might be disconnected, in use, or permissions are wrong.")
            camera = None # Ensure camera is None if opening fails
            return None # Indicate failure

        # Give camera a moment to become ready for reading
        time.sleep(1)

        # Try to read a frame to confirm it's actually working
        ret, _ = camera.read()
        if not ret:
            logger.warning("Webcam opened, but failed to read initial frame. It might not be fully ready or is faulty. Releasing camera.")
            camera.release() # Release if initial read fails
            camera = None
            return None

        logger.info(f"Webcam opened successfully at port {CAMERA_PORT}.")
        return camera
    except Exception as e:
        logger.error(f"Exception during webcam opening: {e}")
        if camera: # Ensure camera is released if exception occurred after creation
            camera.release()
        camera = None
        return None

def generate_frames():
    """Generator function to stream webcam frames as Motion JPEG or a placeholder."""
    global output_frame, lock

    while True:
        cam = get_camera() # Attempt to get camera on each loop iteration (re-try logic)

        if cam is None:
            # If camera is unavailable, stream the placeholder image
            if camera_unavailable_image_bytes:
                with lock:
                    output_frame = None # Clear output_frame when camera is unavailable
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + camera_unavailable_image_bytes + b'\r\n')
                time.sleep(1) # Don't flood with placeholder images, slow down updates
            else:
                logger.error("No camera and no placeholder image. Cannot stream.")
                break # Exit loop if neither camera nor placeholder are available

            # Update status to indicate camera issue
            try:
                analysis_result_queue.put_nowait(("Webcam not found. Please connect and ensure proper mapping.", []))
            except queue.Full:
                pass # Already updated or result pending

            continue # Continue loop to re-attempt camera access

        # If camera is available, proceed with live streaming
        try:
            ret, frame = cam.read()
            if not ret:
                logger.warning("Failed to grab frame from camera (ret=False). Attempting to re-initialize...")
                if camera: # If 'camera' is still not None, explicitly release it
                    camera.release()
                camera = None # Force re-initialization on next loop
                continue # Try again in the next loop iteration
        except Exception as e:
            logger.error(f"Critical error during cam.read(): {e}. Releasing camera and re-initializing.")
            if camera:
                camera.release()
            camera = None # Force re-initialization
            continue # Try again in the next loop iteration

        is_success, buffer = cv2.imencode(".jpg", frame)
        if not is_success:
            logger.error("Failed to encode frame from camera.")
            continue

        with lock:
            output_frame = frame.copy() # Store a copy of the frame for analysis

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

        # Control frame rate for streaming if desired
        # time.sleep(0.03) # Approx 30 FPS, adjust as needed or remove for max FPS

def analyze_frame_thread_worker():
    """Worker function for the analysis thread."""
    global current_hat_status, detected_objects_for_display

    while True:
        try:
            frame_bytes_to_analyze = analysis_queue.get() # Blocks until a frame is available

            try:
                analysis_result_queue.put_nowait(("Analyzing...", []))
            except queue.Full:
                pass # Already updated or result pending

            hat_found_in_frame = False
            objects_in_frame = []

            if computervision_client is None:
                logger.error("Analysis Error: Vision client not initialized. Check API keys.")
                analysis_result_queue.put(("Analysis Error: Vision client not initialized. Check API keys.", []))
                analysis_queue.task_done()
                continue

            # Ensure we're not trying to analyze a placeholder frame
            if camera_unavailable_image_bytes and frame_bytes_to_analyze == camera_unavailable_image_bytes:
                analysis_result_queue.put(("Cannot analyze: Webcam not active or placeholder image.", []))
                analysis_queue.task_done()
                continue

            try:
                image_stream = BytesIO(frame_bytes_to_analyze)
                analysis = computervision_client.analyze_image_in_stream(
                    image_stream, visual_features=[VisualFeatureTypes.objects]
                )

                if analysis.objects:
                    for obj in analysis.objects:
                        # Convert object_property to lowercase for case-insensitive matching
                        if obj.confidence > 0.6 and \
                           ("hat" in obj.object_property.lower() or \
                            "cap" in obj.object_property.lower() or \
                            "headwear" in obj.object_property.lower()):
                            hat_found_in_frame = True
                            objects_in_frame.append(obj)

                if hat_found_in_frame:
                    result_status = "Hat Detected!"
                else:
                    result_status = "No Hat Detected."

                analysis_result_queue.put((result_status, objects_in_frame))

            except Exception as e:
                error_status = f"Analysis Error: {e}"
                analysis_result_queue.put((error_status, []))
                logger.error(f"An error occurred during analysis thread: {e}")
            finally:
                analysis_queue.task_done()

        except Exception as e:
            logger.critical(f"Unexpected critical error in analyze_frame_thread_worker: {e}")

# Start the analysis thread when the app starts. Daemon ensures it exits with main thread.
analysis_thread = threading.Thread(target=analyze_frame_thread_worker, daemon=True)
analysis_thread.start()
logger.info("Analysis thread started.")

@app.route('/')
def index():
    """Serve the main web page."""
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    """Route to stream the webcam video or placeholder."""
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/analyze_frame', methods=['POST'])
def analyze_frame():
    """Endpoint to trigger analysis of the current frame."""
    global output_frame, lock, last_analysis_triggered_time

    # Rate limiting for manual triggers to respect F0 tier
    current_time = time.time()
    F0_MIN_INTERVAL = 3.5 # Minimum 3.5 seconds between requests for F0 tier (20 TPM)
    if (current_time - last_analysis_triggered_time) < F0_MIN_INTERVAL:
        remaining_time = F0_MIN_INTERVAL - (current_time - last_analysis_triggered_time)
        logger.info(f"Rate limit triggered. Remaining time: {remaining_time:.1f}s")
        return jsonify({
            "status": "error",
            "message": f"Rate limit. Please wait {remaining_time:.1f} seconds."
        }), 429
    
    last_analysis_triggered_time = current_time # Update timestamp ONLY if not rate limited

    frame_to_analyze_bytes = None
    with lock:
        # If output_frame is None, it means the camera is not active or no frame captured yet.
        if output_frame is None:
            logger.warning("Attempted analysis with no live frame. output_frame is None.")
            return jsonify({"status": "error", "message": "No live webcam frame available to analyze. Please ensure camera is streaming."}), 503

        # Encode the frame from numpy array to JPEG bytes
        is_success, buffer = cv2.imencode(".jpg", output_frame)
        if not is_success:
            logger.error("Failed to encode current frame for analysis.")
            return jsonify({"status": "error", "message": "Failed to prepare image for analysis."}), 500
        
        frame_to_analyze_bytes = buffer.tobytes()

    # Put the frame into the analysis queue for the background thread
    try:
        # Clear the queue before putting a new item to ensure we analyze the latest frame
        while not analysis_queue.empty():
            analysis_queue.get_nowait()
        analysis_queue.put_nowait(frame_to_analyze_bytes)
        logger.info("Frame sent to analysis queue.")
    except queue.Full:
        logger.warning("Analysis queue is full. Skipping analysis for this frame.")
        return jsonify({"status": "warning", "message": "Analysis in progress, please wait."}), 429
    except Exception as e:
        logger.error(f"Error putting frame into analysis queue: {e}")
        return jsonify({"status": "error", "message": "Internal server error during analysis initiation."}), 500

    # Wait for the analysis result from the background thread
    try:
        # Clear previous results if any, to get the freshest one
        while not analysis_result_queue.empty():
            analysis_result_queue.get_nowait()
        
        # Wait for a result from the analysis thread for up to 60 seconds
        status_message, detected_objects_data = analysis_result_queue.get(timeout=60)
        logger.info(f"Analysis result received: {status_message}")
        
        # Convert detected objects for JSON serialization (bounding boxes)
        serializable_objects = []
        for obj in detected_objects_data:
            serializable_objects.append({
                "object_property": obj.object_property,
                "confidence": obj.confidence,
                "rectangle": {
                    "x": obj.rectangle.x,
                    "y": obj.rectangle.y,
                    "w": obj.rectangle.w,
                    "h": obj.rectangle.h
                }
            })

        return jsonify({
            "status": "success",
            "message": status_message,
            "detected_objects": serializable_objects
        })
    except queue.Empty:
        logger.error("Timeout waiting for analysis result from worker thread.")
        return jsonify({"status": "error", "message": "Analysis timed out. Please try again."}), 500
    except Exception as e:
        logger.error(f"Error retrieving analysis result: {e}")
        return jsonify({"status": "error", "message": f"Failed to get analysis results: {e}"}), 500
