FROM python:3.11-slim

# Install system dependencies for Bluetooth D-Bus access & rclone cloud sync
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    dbus \
    rclone \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cycplus_bridge.py .

CMD ["python", "-u", "cycplus_bridge.py"]
