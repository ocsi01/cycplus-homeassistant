import asyncio
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
import fitparse
import paho.mqtt.client as mqtt
from bleak import BleakClient, BleakScanner

# ================= Configuration =================
# Replace with your Cycplus device name (e.g., M2_XXXX) and Bluetooth MAC address
TARGET_NAME = os.getenv("TARGET_NAME", "M2_XXXX")
TARGET_MAC = os.getenv("TARGET_MAC", "AA:BB:CC:11:22:33")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/app/rides")
WWW_DIR = os.getenv("WWW_DIR", "/mnt/Container/hass/www")

# Replace with your MQTT broker IP address
MQTT_BROKER = os.getenv("MQTT_BROKER", "192.168.1.22")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER", "ha_mqtt")
MQTT_PASS = os.getenv("MQTT_PASS", "hamqtt")
MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "cycplus_m2")

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", 15))

# Google Drive Cloud Backup Configuration (replace with your folder ID)
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "YOUR_GOOGLE_DRIVE_FOLDER_ID")
GDRIVE_CREDS_FILE = os.getenv("GDRIVE_CREDS_FILE", "")
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "gdrive")
RCLONE_CONF_FILE = os.getenv("RCLONE_CONF_FILE", "")
# =================================================

CHAR_CTRL = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"  # Control channel
CHAR_TX   = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Notifications from device
CHAR_RX   = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Commands to device
CHAR_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"

CMD_FILELIST = bytearray([0x05, 0x66, 0x69, 0x6c, 0x65, 0x6c, 0x69, 0x73, 0x74, 0x2e, 0x74, 0x78, 0x74, 0x57])
CMD_DISKSPACE = bytearray([0x09, 0x00, 0x09])
CMD_READ_PERM = bytearray([0xff, 0x00, 0xff])
CMD_COPY = bytearray([0x43])
CMD_COPY_OK = bytearray([0x06])
CMD_COPY_FINISH = bytearray([0x15])
AWAIT_MARKER = bytearray([0x41, 0x77, 0x61, 0x69, 0x74, 0x4e, 0x65, 0x77, 0x44, 0x61, 0x74, 0x61])

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CycplusBridge")


class RcloneBackup:
    """Manages Google Drive backups using rclone with built-in OAuth (zero GCP setup)."""
    def __init__(self, remote_name="gdrive", folder_id=None, config_override=None):
        self.remote_name = remote_name or os.getenv("RCLONE_REMOTE", "gdrive")
        self.folder_id = folder_id or GDRIVE_FOLDER_ID
        self.enabled = False
        self.conf_file = None
        self.last_backup_time = None
        self.uploaded_count = 0
        self.rclone_bin = shutil.which("rclone") or "/usr/bin/rclone"

        search_paths = []
        if config_override:
            search_paths.append(config_override)
        search_paths.extend([
            os.getenv("RCLONE_CONFIG"),
            "/app/rclone.conf",
            "/mnt/Container/cycplus/rclone.conf",
            "./rclone.conf",
            os.path.expanduser("~/.config/rclone/rclone.conf")
        ])

        for p in search_paths:
            if p and os.path.exists(p):
                self.conf_file = p
                self.enabled = True
                break

        if self.enabled:
            logger.info(f"✅ rclone Google Drive configuration detected ({self.conf_file})")
        else:
            logger.info("ℹ️ rclone backup: no rclone.conf found. (Place rclone.conf in /mnt/Container/cycplus/rclone.conf)")

    def _get_base_cmd(self):
        cmd = [self.rclone_bin]
        if self.conf_file:
            cmd.extend(["--config", self.conf_file])
        return cmd

    def _get_remote_dest(self):
        # Target remote
        return f"{self.remote_name}:"

    def upload_file(self, filepath):
        if not self.enabled:
            return False
        filename = os.path.basename(filepath)
        try:
            dest = f"{self._get_remote_dest()}{filename}"
            cmd = self._get_base_cmd() + ["copyto", filepath, dest, "-q"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode == 0:
                self.last_backup_time = datetime.now(timezone.utc).isoformat()
                self.uploaded_count += 1
                logger.info(f"☁️ [rclone] Successfully backed up {filename} to Google Drive")
                return True
            else:
                logger.error(f"❌ [rclone] Failed to upload {filename}: {res.stderr.strip()}")
                return False
        except Exception as e:
            logger.error(f"❌ [rclone] Upload error: {e}")
            return False

    def sync_local_directory(self, download_dir):
        if not self.enabled:
            return 0
        try:
            dest = self._get_remote_dest()
            cmd = self._get_base_cmd() + [
                "copy", download_dir, dest,
                "--include", "*.fit",
                "--stats-one-line"
            ]
            logger.info(f"☁️ [rclone] Syncing {download_dir} to Google Drive ({dest})...")
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if res.returncode == 0:
                self.last_backup_time = datetime.now(timezone.utc).isoformat()
                logger.info("☁️ [rclone] Google Drive directory sync completed.")
                return 1
            else:
                logger.error(f"❌ [rclone] Sync failed: {res.stderr.strip()}")
                return 0
        except Exception as e:
            logger.error(f"❌ [rclone] Sync error: {e}")
            return 0


class GDriveBackup:
    """Manages headless Google Drive backups using Service Account or OAuth credentials."""
    def __init__(self, folder_id, creds_override=None):
        self.folder_id = folder_id
        self.service = None
        self.enabled = False
        self.creds_file = None
        self.last_backup_time = None
        self.uploaded_count = 0

        search_paths = []
        if creds_override:
            search_paths.append(creds_override)
        search_paths.extend([
            "/app/service_account.json",
            "/app/credentials.json",
            "/mnt/Container/cycplus/service_account.json",
            "/mnt/Container/cycplus/credentials.json",
            "./service_account.json",
            "./credentials.json"
        ])

        for p in search_paths:
            if os.path.exists(p):
                self.creds_file = p
                break

        if self.creds_file and self.folder_id:
            self.init_client()
        else:
            logger.info("ℹ️ Google Drive backup: no credentials.json / service_account.json found. Cloud backup dormant.")

    def init_client(self):
        try:
            # 1. Try Service Account Key (Recommended for 24/7 background servers)
            try:
                from google.oauth2 import service_account
                from googleapiclient.discovery import build

                creds = service_account.Credentials.from_service_account_file(
                    self.creds_file,
                    scopes=['https://www.googleapis.com/auth/drive.file']
                )
                self.service = build('drive', 'v3', credentials=creds, cache_discovery=False)
                self.enabled = True
                logger.info(f"✅ Google Drive authenticated via Service Account ({self.creds_file})")
                return
            except Exception as sa_err:
                logger.debug(f"Not a Service Account JSON ({sa_err}), attempting OAuth...")

            # 2. Try User OAuth credentials (with refresh token)
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(
                self.creds_file,
                scopes=['https://www.googleapis.com/auth/drive.file']
            )
            self.service = build('drive', 'v3', credentials=creds, cache_discovery=False)
            self.enabled = True
            logger.info("✅ Google Drive authenticated via User OAuth Credentials")
        except Exception as e:
            logger.warning(f"Failed to initialize Google Drive client: {e}")
            self.enabled = False

    def list_remote_files(self):
        if not self.enabled or not self.service:
            return set()
        try:
            query = f"'{self.folder_id}' in parents and trashed = false"
            results = self.service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name, size)',
                pageSize=1000
            ).execute()
            files = results.get('files', [])
            return {f['name'] for f in files}
        except Exception as e:
            logger.error(f"Error querying Google Drive folder: {e}")
            return set()

    def upload_file(self, filepath):
        if not self.enabled or not self.service:
            return False
        filename = os.path.basename(filepath)
        try:
            from googleapiclient.http import MediaFileUpload
            file_metadata = {
                'name': filename,
                'parents': [self.folder_id]
            }
            media = MediaFileUpload(filepath, mimetype='application/octet-stream', resumable=True)
            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, name'
            ).execute()
            self.last_backup_time = datetime.now(timezone.utc).isoformat()
            self.uploaded_count += 1
            logger.info(f"☁️ Successfully backed up {filename} to Google Drive (ID: {file.get('id')})")
            return True
        except Exception as e:
            logger.error(f"Failed to upload {filename} to Google Drive: {e}")
            return False

    def sync_local_directory(self, download_dir):
        if not self.enabled:
            return 0
        try:
            remote_filenames = self.list_remote_files()
            local_files = sorted(glob.glob(os.path.join(download_dir, "*.fit")))
            synced = 0
            for local_path in local_files:
                fname = os.path.basename(local_path)
                if fname not in remote_filenames and os.path.getsize(local_path) > 100:
                    logger.info(f"Uploading missing file to Drive: {fname}")
                    if self.upload_file(local_path):
                        synced += 1
                        time.sleep(0.5)
            if synced > 0:
                logger.info(f"☁️ Synced {synced} files to Google Drive.")
            return synced
        except Exception as e:
            logger.error(f"Error during Google Drive initial sync: {e}")
            return 0


class CycplusBridge:
    def __init__(self):
        self.download_dir = DOWNLOAD_DIR
        os.makedirs(self.download_dir, exist_ok=True)
        self.mqtt_client = None
        self.reset_transfer_state()

        # Google Drive Cloud Backup (supports rclone or direct GCP API)
        self.rclone = RcloneBackup(remote_name=RCLONE_REMOTE, folder_id=GDRIVE_FOLDER_ID, config_override=RCLONE_CONF_FILE)
        self.gdrive = GDriveBackup(GDRIVE_FOLDER_ID, GDRIVE_CREDS_FILE)

        if self.rclone.enabled:
            self.cloud = self.rclone
            self.cloud_provider = "rclone"
        elif self.gdrive.enabled:
            self.cloud = self.gdrive
            self.cloud_provider = "gcp_service_account"
        else:
            self.cloud = self.rclone
            self.cloud_provider = "none"

        self.init_mqtt()
        self.publish_all_rides_to_ha()

        # Initial check & sync of local rides to Drive
        if self.cloud.enabled:
            self.cloud.sync_local_directory(self.download_dir)
            self.publish_gdrive_status()

    def init_mqtt(self):
        try:
            self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "cycplus_m2_bridge")
            self.mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
            self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            self.mqtt_client.loop_start()
            logger.info("Connected to MQTT Broker")
            self.publish_ha_discovery()
            self.publish_state("status", "Idle (Waiting for bike)")
            self.publish_gdrive_status()
        except Exception as e:
            logger.error(f"MQTT initialization error: {e}")

    def publish_ha_discovery(self):
        device_info = {
            "identifiers": [f"cycplus_m2_{TARGET_MAC.replace(':', '')}"],
            "name": f"Cycplus M2 ({TARGET_NAME})",
            "model": "CYCPLUS M2",
            "manufacturer": "CDZN Tech Co.,Ltd",
            "sw_version": "1.3.8 / V1.38"
        }

        sensors = [
            {
                "name": "Status",
                "id": "status",
                "state_topic": f"{MQTT_TOPIC_PREFIX}/status",
                "icon": "mdi:bike"
            },
            {
                "name": "Battery",
                "id": "battery",
                "state_topic": f"{MQTT_TOPIC_PREFIX}/battery",
                "unit_of_measurement": "%",
                "device_class": "battery"
            },
            {
                "name": "Last Sync",
                "id": "last_sync",
                "state_topic": f"{MQTT_TOPIC_PREFIX}/last_sync",
                "device_class": "timestamp",
                "icon": "mdi:cloud-check"
            },
            {
                "name": "Total Rides on Device",
                "id": "total_rides",
                "state_topic": f"{MQTT_TOPIC_PREFIX}/total_rides",
                "unit_of_measurement": "rides",
                "icon": "mdi:counter"
            },
            {
                "name": "Free Disk Space",
                "id": "disk_free",
                "state_topic": f"{MQTT_TOPIC_PREFIX}/disk_free",
                "json_attributes_topic": f"{MQTT_TOPIC_PREFIX}/disk_free_attr",
                "unit_of_measurement": "KB",
                "icon": "mdi:harddisk"
            },
            {
                "name": "Last Ride",
                "id": "last_ride",
                "state_topic": f"{MQTT_TOPIC_PREFIX}/last_ride",
                "json_attributes_topic": f"{MQTT_TOPIC_PREFIX}/last_ride_attr",
                "icon": "mdi:map-marker-distance"
            },
            {
                "name": "Downloaded Rides List",
                "id": "rides_list",
                "state_topic": f"{MQTT_TOPIC_PREFIX}/rides_list",
                "json_attributes_topic": f"{MQTT_TOPIC_PREFIX}/rides_list_attr",
                "unit_of_measurement": "rides",
                "icon": "mdi:format-list-bulleted"
            },
            {
                "name": "Google Drive Backup",
                "id": "gdrive_status",
                "state_topic": f"{MQTT_TOPIC_PREFIX}/gdrive_status",
                "json_attributes_topic": f"{MQTT_TOPIC_PREFIX}/gdrive_status_attr",
                "icon": "mdi:google-drive"
            }
        ]

        for s in sensors:
            cfg_topic = f"homeassistant/sensor/{MQTT_TOPIC_PREFIX}/{s['id']}/config"
            payload = {
                "name": s["name"],
                "unique_id": f"cycplus_m2_{s['id']}",
                "state_topic": s["state_topic"],
                "device": device_info
            }
            if "unit_of_measurement" in s:
                payload["unit_of_measurement"] = s["unit_of_measurement"]
            if "device_class" in s:
                payload["device_class"] = s["device_class"]
            if "icon" in s:
                payload["icon"] = s["icon"]
            if "json_attributes_topic" in s:
                payload["json_attributes_topic"] = s["json_attributes_topic"]
            self.mqtt_client.publish(cfg_topic, json.dumps(payload), retain=True)

        logger.info("Published Home Assistant MQTT Auto-Discovery topics.")

    def publish_state(self, sensor_id, value):
        if self.mqtt_client:
            topic = f"{MQTT_TOPIC_PREFIX}/{sensor_id}"
            self.mqtt_client.publish(topic, str(value), retain=True)
            logger.info(f"MQTT -> {topic}: {value}")

    def publish_attributes(self, sensor_id, attributes: dict):
        if self.mqtt_client:
            topic = f"{MQTT_TOPIC_PREFIX}/{sensor_id}_attr"
            self.mqtt_client.publish(topic, json.dumps(attributes), retain=True)

    def publish_gdrive_status(self):
        state = "Active" if self.cloud.enabled else "Not Configured"
        self.publish_state("gdrive_status", state)
        attrs = {
            "enabled": self.cloud.enabled,
            "provider": self.cloud_provider,
            "folder_id": GDRIVE_FOLDER_ID,
            "folder_url": f"https://drive.google.com/drive/u/0/folders/{GDRIVE_FOLDER_ID}",
            "last_backup_time": self.cloud.last_backup_time or "None",
            "backed_up_count": self.cloud.uploaded_count
        }
        if hasattr(self.cloud, "conf_file") and self.cloud.conf_file:
            attrs["config_file"] = self.cloud.conf_file
        elif hasattr(self.cloud, "creds_file") and self.cloud.creds_file:
            attrs["config_file"] = self.cloud.creds_file
        self.publish_attributes("gdrive_status", attrs)

    def parse_fit_file(self, filepath):
        try:
            fitfile = fitparse.FitFile(filepath)
            data = {}
            for record in fitfile.get_messages("session"):
                for field in record:
                    if field.name in ["start_time", "total_distance", "total_timer_time", "avg_speed", "max_speed", "total_ascent", "total_calories", "avg_heart_rate"]:
                        data[field.name] = field.value

            dist_km = round((data.get("total_distance") or 0) / 1000.0, 2)
            dur_s = int(data.get("total_timer_time") or 0)
            mins = dur_s // 60
            secs = dur_s % 60
            dur_str = f"{mins}m {secs}s" if mins < 60 else f"{mins // 60}h {mins % 60}m"
            avg_spd = round((data.get("avg_speed") or 0) * 3.6, 1)
            max_spd = round((data.get("max_speed") or 0) * 3.6, 1)
            st_raw = data.get("start_time")
            st_str = st_raw.strftime("%Y-%m-%d %H:%M:%S") if st_raw else os.path.basename(filepath)

            return {
                "filename": os.path.basename(filepath),
                "date": st_str,
                "distance_km": dist_km,
                "duration": dur_str,
                "duration_seconds": dur_s,
                "avg_speed_kmh": avg_spd,
                "max_speed_kmh": max_spd,
                "ascent_m": data.get("total_ascent") or 0,
                "calories": data.get("total_calories") or 0,
                "size_kb": round(os.path.getsize(filepath) / 1024.0, 1)
            }
        except Exception as e:
            logger.warning(f"Error parsing FIT {filepath}: {e}")
            return {
                "filename": os.path.basename(filepath),
                "date": os.path.basename(filepath),
                "distance_km": 0,
                "duration": "N/A",
                "avg_speed_kmh": 0,
                "size_kb": round(os.path.getsize(filepath) / 1024.0, 1)
            }

    def publish_all_rides_to_ha(self):
        fit_paths = sorted(glob.glob(os.path.join(self.download_dir, "*.fit")), reverse=True)
        rides = []
        for p in fit_paths:
            rides.append(self.parse_fit_file(p))

        self.publish_state("rides_list", len(rides))
        self.publish_attributes("rides_list", {"rides": rides})

        if rides:
            latest = rides[0]
            self.publish_state("last_ride", f"{latest['distance_km']} km ({latest['duration']})")
            self.publish_attributes("last_ride", latest)
        self.export_rides_geo(rides, fit_paths)
        logger.info(f"Published {len(rides)} parsed rides to Home Assistant.")

    def export_rides_geo(self, parsed_rides, fit_paths):
        try:
            geo_rides = []
            for ride, filepath in zip(parsed_rides, fit_paths):
                coords = []
                try:
                    fit = fitparse.FitFile(filepath)
                    for r in fit.get_messages("record"):
                        lat, lon = None, None
                        for f in r:
                            if f.name == "position_lat" and f.value is not None:
                                lat = f.value * (180.0 / 2**31)
                            elif f.name == "position_long" and f.value is not None:
                                lon = f.value * (180.0 / 2**31)
                        if lat is not None and lon is not None and abs(lat) > 0.01:
                            coords.append([round(lat, 6), round(lon, 6)])
                except Exception as ex:
                    logger.warning(f"Failed to extract GPS from {filepath}: {ex}")

                step = max(1, len(coords) // 500)
                sampled = coords[::step]
                if coords and (not sampled or sampled[-1] != coords[-1]):
                    sampled.append(coords[-1])

                geo_rides.append({
                    "filename": ride["filename"],
                    "date": ride["date"],
                    "distance_km": ride["distance_km"],
                    "duration": ride["duration"],
                    "avg_speed_kmh": ride["avg_speed_kmh"],
                    "points_count": len(sampled),
                    "coords": sampled
                })

            # Save in download parent directory
            geo_file = os.path.join(os.path.dirname(self.download_dir), "rides_geo.json")
            with open(geo_file, "w") as f:
                json.dump(geo_rides, f)
            logger.info(f"Auto-exported {len(geo_rides)} GPS routes to {geo_file}")

            # Also update Home Assistant www folder if mounted/configured
            if WWW_DIR and os.path.exists(WWW_DIR):
                ha_geo = os.path.join(WWW_DIR, "rides_geo.json")
                try:
                    with open(ha_geo, "w") as f:
                        json.dump(geo_rides, f)
                    logger.info(f"Updated Home Assistant web directory: {ha_geo}")
                except Exception as ex:
                    logger.warning(f"Could not copy to {ha_geo}: {ex}")

        except Exception as e:
            logger.error(f"Error exporting rides_geo: {e}")

    def reset_transfer_state(self):
        self.combine = False
        self.reply_ok = False
        self.data = bytearray()
        self.file_check = bytearray()
        self.trigger = False
        self.chunk_count = 0
        self.last_notification = bytearray()

    def notification_handler(self, sender, data: bytearray):
        self.last_notification = data
        sender_str = str(sender).lower()

        if CHAR_CTRL in sender_str:
            if data == bytearray(b"\x04"):
                return
            file_data = data[:-1]
            if file_data == self.file_check:
                self.reply_ok = True
            return

        if data == bytearray(b"\x04"):
            self.combine = False
            self.chunk_count = 6
            return

        if self.trigger:
            data = data[3:]
            self.trigger = False

        if self.combine:
            self.data.extend(data)
            self.chunk_count += 1

    async def wait_for_data(self, timeout=10.0):
        intervals = int(timeout * 100)
        for _ in range(intervals):
            if self.last_notification != AWAIT_MARKER:
                return True
            await asyncio.sleep(0.01)
        return False

    async def send_cmd(self, client: BleakClient, char_uuid, payload, delay=0.02):
        await client.write_gatt_char(char_uuid, payload, response=False)
        await asyncio.sleep(delay)

    async def request_read_permission(self, client: BleakClient):
        self.last_notification = AWAIT_MARKER
        await self.send_cmd(client, CHAR_CTRL, CMD_READ_PERM)
        return await self.wait_for_data(timeout=5.0)

    async def sync_device_session(self, client: BleakClient):
        self.publish_state("status", "Connected (Syncing...)")
        await asyncio.sleep(1.0)

        # 1. Read Battery
        try:
            bat_raw = await client.read_gatt_char(CHAR_BATTERY)
            if len(bat_raw) > 0:
                battery_pct = bat_raw[0]
                self.publish_state("battery", battery_pct)
        except Exception as e:
            logger.warning(f"Could not read battery: {e}")

        # 2. Read Free Disk Space
        self.last_notification = AWAIT_MARKER
        await self.send_cmd(client, CHAR_CTRL, CMD_DISKSPACE)
        if await self.wait_for_data(timeout=5.0):
            if self.last_notification.startswith(b"\n"):
                disk_str = self.last_notification[1:-1].decode("utf-8", errors="ignore").strip()
                if "/" in disk_str:
                    parts = disk_str.split("/")
                    free_kb = int(parts[0].strip())
                    total_kb = int(parts[1].strip())
                    self.publish_state("disk_free", free_kb)
                    self.publish_attributes("disk_free", {
                        "free_kb": free_kb,
                        "total_kb": total_kb,
                        "used_kb": total_kb - free_kb,
                        "free_percent": round((free_kb / total_kb) * 100, 1),
                        "summary": disk_str
                    })
                else:
                    self.publish_state("disk_free", disk_str)

        await asyncio.sleep(1.0)

        # 3. Read Filelist
        logger.info("Requesting filelist...")
        if not await self.request_read_permission(client):
            logger.error("Failed read permission for filelist")
            return 0

        self.last_notification = AWAIT_MARKER
        await self.send_cmd(client, CHAR_CTRL, CMD_FILELIST)
        await self.wait_for_data()

        # Copy handshake
        self.last_notification = AWAIT_MARKER
        await self.send_cmd(client, CHAR_RX, CMD_COPY)
        await self.wait_for_data()
        await self.send_cmd(client, CHAR_RX, CMD_COPY_OK)

        self.data = bytearray()
        self.combine = True
        self.trigger = True
        await self.send_cmd(client, CHAR_RX, CMD_COPY)

        while self.combine:
            while self.chunk_count <= 5 and self.combine:
                await asyncio.sleep(0.01)
            self.chunk_count = 0
            self.data = self.data[:-2]
            self.trigger = True
            await self.send_cmd(client, CHAR_RX, CMD_COPY_OK)

        self.last_notification = AWAIT_MARKER
        await self.send_cmd(client, CHAR_RX, CMD_COPY_FINISH)
        await self.wait_for_data()
        await self.send_cmd(client, CHAR_RX, CMD_COPY_OK)
        await self.wait_for_data()

        text = self.data.decode("utf-8", errors="ignore")
        pattern = re.compile(r"\d{14}\.fit")
        found_files = sorted(list(set(pattern.findall(text))))
        self.publish_state("total_rides", len(found_files))
        logger.info(f"Rides on M2: {found_files}")

        # Pause to let Cycplus M2 close the filelist transfer cleanly
        await asyncio.sleep(2.0)

        # 4. Download any new files
        new_downloads = 0
        for fit_file in found_files:
            local_path = os.path.join(self.download_dir, fit_file)

            # Check if file already exists locally and has non-zero size
            if os.path.exists(local_path) and os.path.getsize(local_path) > 100:
                logger.info(f"⏭️ {fit_file} already exists ({os.path.getsize(local_path)} bytes), skipping.")
                continue

            self.publish_state("status", f"Downloading {fit_file}...")

            # Download with up to 2 retries
            success = False
            for attempt in range(1, 3):
                success = await self.download_single_file(client, fit_file, local_path)
                if success:
                    new_downloads += 1
                    # Backup to Google Drive immediately upon download!
                    if self.cloud.enabled:
                        self.cloud.upload_file(local_path)
                        self.publish_gdrive_status()
                    break
                logger.warning(f"Retrying download for {fit_file} (attempt {attempt + 1})...")
                await asyncio.sleep(2.0)

            await asyncio.sleep(1.5)

        now_iso = datetime.now(timezone.utc).isoformat()
        self.publish_state("last_sync", now_iso)

        # Re-parse all rides and update list in Home Assistant
        self.publish_all_rides_to_ha()

        if new_downloads > 0:
            self.publish_state("status", f"Synced ({new_downloads} new rides)")
        else:
            self.publish_state("status", "Synced (Up to date)")
        return new_downloads

    async def download_single_file(self, client: BleakClient, filename, local_path):
        self.reset_transfer_state()
        byte_req = bytearray([0x05]) + bytearray(filename, "utf-8") + bytearray([0x50])
        self.file_check = bytearray([0x06]) + bytearray(filename, "utf-8")

        if not await self.request_read_permission(client):
            logger.warning(f"Could not get read permission for {filename}")
            return False

        self.last_notification = AWAIT_MARKER
        await self.send_cmd(client, CHAR_CTRL, byte_req)
        if not await self.wait_for_data(timeout=8.0) or not self.reply_ok:
            logger.warning(f"Device did not ACK request for {filename}")
            return False

        self.combine = False
        self.last_notification = AWAIT_MARKER
        await self.send_cmd(client, CHAR_RX, CMD_COPY)
        await self.wait_for_data()
        await self.send_cmd(client, CHAR_RX, CMD_COPY_OK)

        self.data = bytearray()
        self.combine = True
        self.trigger = True
        await self.send_cmd(client, CHAR_RX, CMD_COPY)

        transfer_timeout = 0
        last_len = 0
        stall = 0

        while self.combine:
            while self.chunk_count <= 5 and self.combine:
                await asyncio.sleep(0.01)
            self.chunk_count = 0
            self.data = self.data[:-2]
            self.trigger = True
            await self.send_cmd(client, CHAR_RX, CMD_COPY_OK)

            if len(self.data) > last_len:
                last_len = len(self.data)
                stall = 0
            else:
                stall += 1

            transfer_timeout += 1
            if transfer_timeout > 3000 or stall > 150:
                logger.error(f"Transfer stalled for {filename}")
                break

        self.last_notification = AWAIT_MARKER
        await self.send_cmd(client, CHAR_RX, CMD_COPY_FINISH)
        await self.wait_for_data()
        await self.send_cmd(client, CHAR_RX, CMD_COPY_OK)
        await self.wait_for_data()

        raw = self.data
        while raw and raw[-1] == 0x00:
            raw = raw[:-1]

        if len(raw) > 100:
            with open(local_path, "wb") as f:
                f.write(raw)
            logger.info(f"✅ Successfully saved {filename} ({len(raw)} bytes)")
            return True
        return False

    async def main_loop(self):
        logger.info(f"🚀 Cycplus M2 Smart Arrival Bridge started. Watching for {TARGET_NAME} ({TARGET_MAC})...")
        presence_state = "AWAY"
        missed_scans = 0
        empty_sync_count = 0

        while True:
            try:
                device = await BleakScanner.find_device_by_address(TARGET_MAC, timeout=6.0)

                if device:
                    missed_scans = 0
                    if presence_state == "PRESENT_SYNCED":
                        # Bike is parked and already synced.
                        # Do NOT open GATT connection to avoid draining battery!
                        await asyncio.sleep(25.0)
                        continue

                    logger.info(f"🚴 Detected {TARGET_NAME}! Initiating arrival sync session...")
                    self.publish_state("status", "Connecting & Syncing...")
                    new_downloads = 0

                    async with BleakClient(device, timeout=20.0) as client:
                        if client.is_connected:
                            logger.info(f"Connected to {TARGET_NAME}")
                            await asyncio.sleep(1.0)
                            await client.start_notify(CHAR_CTRL, self.notification_handler)
                            await client.start_notify(CHAR_TX, self.notification_handler)
                            new_downloads = await self.sync_device_session(client)
                            await client.stop_notify(CHAR_CTRL)
                            await client.stop_notify(CHAR_TX)
                            logger.info("Session complete. Disconnected cleanly.")

                    if new_downloads and new_downloads > 0:
                        presence_state = "PRESENT_SYNCED"
                        empty_sync_count = 0
                        self.publish_state("status", f"Home (Synced: {new_downloads} new rides)")
                        logger.info("✅ Synced new ride(s). Entering sleep lock until next departure.")
                    else:
                        empty_sync_count += 1
                        if empty_sync_count < 3:
                            logger.info(f"No new rides in filelist yet ({empty_sync_count}/3). Rider might still be saving. Retrying in 30s...")
                            self.publish_state("status", "Waiting for ride to be saved / stopped...")
                            await asyncio.sleep(30.0)
                        else:
                            presence_state = "PRESENT_SYNCED"
                            self.publish_state("status", "Home (Synced)")
                            logger.info("Already up to date. Entering sleep lock until next departure.")
                            await asyncio.sleep(25.0)
                else:
                    missed_scans += 1
                    if presence_state == "PRESENT_SYNCED" and missed_scans >= 3:
                        presence_state = "AWAY"
                        empty_sync_count = 0
                        self.publish_state("status", "Standby (Waiting for bike)")
                        logger.info("M2 powered off or departed. Bridge armed for next arrival.")

                    await asyncio.sleep(SCAN_INTERVAL_SECONDS)

            except Exception as e:
                logger.warning(f"Scan/Sync loop notice: {e}")
                self.publish_state("status", "Standby (Waiting for bike)")
                await asyncio.sleep(10.0)


if __name__ == "__main__":
    bridge = CycplusBridge()
    asyncio.run(bridge.main_loop())
