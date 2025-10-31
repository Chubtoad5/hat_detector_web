
# Object Detector Application

This is a python based application that leverages Azure Computer Vision (https://azure.microsoft.com/en-us/resources/cloud-computing-dictionary/what-is-computer-vision)
The application is deployed on Ubuntu and taps into a local USB or RTSP camera feed for analyzing frames with the Computer Vision APIs

## OVERVIEW

This guide explains how to deploy the Objec Detector application on a new server using the all-in-one deployment script (`deploy.sh`). The script automates the installation of all dependencies, configures the necessary services (web app and camera manager), and starts the application.

This applications takes a local video feed (i.e. from a USB Webcam, presented via /dev/video0) and from an RTSP URL provided during setup.

## PREREQUISITES

1. Sudo (root) access on the server.
2. The `deploy.sh` script and your `camera_unavailable.jpg` image file.
3. Your **Azure Computer Vision credentials** (Key and Endpoint).
4. The URL for your **RTSP stream** (if you plan to use it).
5. A USB camera plugged in and accessible via /dev/video0
6. When using USB camera, it is recommended to update the system and rebooting before installing to avoid potential driver issues

```
sudo apt update && sudo apt upgrade -y
```

## DEPLOYMENT STEPS

1. Download `deploy.sh` and `camera_unavailable.jpg` from this repo, or clone the repo
```
git clone https://github.com/Chubtoad5/object_detector_web.git
```
2. Make `deploy.sh` executable
```
cd object_detector_web
chmod +x deploy.sh
```
3. Edit `deploy.sh` and update AZURE_VISION_KEY and AZURE_VISION_ENDPOINT with valid credentials, optionally update RTSP_URL if using an accessible RTSP stream.

4. Run the deploy script
```
sudo ./deploy.sh
```

Since the app supports frontend variables, it can be installed with one line:
```
sudo -s
git clone https://github.com/Chubtoad5/object_detector_web.git; cd object_detector_web; chmod +x deploy.sh; AZURE_VISION_KEY="MY_KEY" AZURE_VISION_ENDPOINT="MY_ENDPOINT" RTSP_URL="rtsp://my_rtsp/url" ./deploy.sh
```


## ACCESSING THE APPLICATION

Once the deployment script is finished, the application will be running and accessible on port 80 (standard HTTP).

1.  Find your server's IP address:
    ```
    hostname -I
    ```

2.  Open a web browser on a computer on the same network and navigate to:
    ```
    http://<your_server_ip>
    ```

You should now see the object Detector application's user interface.
