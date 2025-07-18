======================================
Hat Detector Application - Deployment Guide
======================================

OVERVIEW
--------
This guide explains how to deploy the Hat Detector application on a new server using the all-in-one deployment script (`deploy.sh`). The script automates the installation of all dependencies, configures the necessary services (web app and camera manager), and starts the application.

PREREQUISITES
-------------
1. A server running Ubuntu 22.04.
2. Sudo (root) access on the server.
3. The `deploy.sh` script and your `camera_unavailable.jpg` image file.
4. Your **Azure Computer Vision credentials** (Key and Endpoint).
5. The URL for your **RTSP stream** (if you plan to use it).

DEPLOYMENT STEPS
----------------
1.  Place the `deploy.sh` script and the `camera_unavailable.jpg` file in the same directory on your new server (e.g., in your home directory).

2.  Open a terminal and navigate to that directory.

3.  Make the script executable:
    ```
    chmod +x deploy.sh
    ```

4.  Run the script with sudo:
    ```
    sudo ./deploy.sh
    ```
    The script will now set up everything automatically. It will take a few minutes to complete.

CONFIGURATION (IMPORTANT)
-------------------------
The deployment script sets up the application with default values. For the application to be fully functional, you must configure your credentials. The recommended way is to edit the systemd service files **after** the deployment is complete.

### 1. Configure Azure Credentials

This is required for the "Analyze Frame" button to work.

* **File to Edit:** `/etc/systemd/system/hat-detector.service`
* **Command to open the file for editing:**
    ```
    sudo systemctl edit --full hat-detector.service
    ```
* **Action:** In the `[Service]` section, add/uncomment the `Environment=` lines and fill in your details:
    ```ini
    [Service]
    Environment="AZURE_VISION_SUBSCRIPTION_KEY=PASTE_YOUR_KEY_HERE"
    Environment="AZURE_VISION_ENDPOINT=PASTE_YOUR_ENDPOINT_HERE"
    ...
    ```
* **Apply Changes:**
    ```
    sudo systemctl restart hat-detector.service
    ```

### 2. Configure RTSP Stream URL

This is required for the "Use RTSP Stream" button to work.

* **File to Edit:** `/etc/systemd/system/camera.service`
* **Command to open the file for editing:**
    ```
    sudo systemctl edit --full camera.service
    ```
* **Action:** In the `[Service]` section, add the `Environment=` line with your RTSP URL. If this line is not present, the script will use a hardcoded default.
    ```ini
    [Service]
    Environment="RTSP_STREAM_URL=rtsp://your.camera.ip/stream/path"
    ...
    ```
* **Apply Changes:**
    ```
    sudo systemctl restart camera.service
    ```

ACCESSING THE APPLICATION
-------------------------
Once the deployment script is finished, the application will be running and accessible on port 80 (standard HTTP).

1.  Find your server's IP address:
    ```
    hostname -I
    ```

2.  Open a web browser on a computer on the same network and navigate to:
    ```
    http://<your_server_ip>
    ```

You should now see the Hat Detector application's user interface.
