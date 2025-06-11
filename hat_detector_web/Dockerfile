# Use an official Python runtime as a parent image
FROM python:3.9-slim-buster

# Set the working directory in the container
WORKDIR /app

# Install system dependencies for OpenCV (libgl1-mesa-glx) and Flask (Gunicorn)
# ffmpeg is useful for video processing, build-essential for compiling some Python packages.
# libgirepository1.0-dev can help with some GUI-related issues if present, though often not strictly needed for headless OpenCV.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libgirepository1.0-dev \
    build-essential \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed Python packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port that the Flask app will run on
EXPOSE 5000

# Command to run the Flask application using Gunicorn
# -w: number of worker processes (tune based on CPU cores, often 2*CPU + 1)
# -b: bind address
# app:app (module:instance)
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:5000", "app:app"]
# We start with 1 worker to minimize webcam contention. Increase workers carefully.