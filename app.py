import os
import requests
import json
import time
import threading
import traceback
import urllib3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, HTTPServer

# Suppress insecure HTTPS warnings for the classic controller
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIGURATION ---
API_KEY = os.environ.get("UNIFI_API_KEY")
MODERN_URL = "https://api.ui.com/v1"

CLASSIC_URL = os.environ.get("CLASSIC_URL", "https://emerald.unificloud.co.uk:8443")
CLASSIC_USER = os.environ.get("CLASSIC_USER")
CLASSIC_PASS = os.environ.get("CLASSIC_PASS")

DATA_FILE = "data.json"
STATE_FILE = "alerts.json" # Tracks which devices we have already emailed about
POLL_INTERVAL = 300 
ALERT_WINDOW_MINS = 240 

# --- SMTP EMAIL CONFIGURATION ---
SMTP_SERVER = "192.168.242.2"
SMTP_PORT = 25
SMTP_USER = "alerts@emerald-group.local"
SMTP_PASS = "CrazyF00l"
EMAIL_FROM = "unifi-alerts@emerald-group.co.uk"
EMAIL_TO = "alerts@emerald-group.co.uk"
ALERT_THRESHOLD_SECONDS = 8 * 3600  # 8 Hours

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def send_offline_alert(site_name, device_name, device_model, offline_duration):
    """Sends an SMTP email alert for a device that has been offline > 8 hours."""
    subject = f"URGENT: UniFi Device Offline > 8 Hours - {site_name} ({device_name})"
    
    body = f"""Hello,

This is an automated alert from the UniFi Monitor.

A network device has been offline for more than 8 hours:
- Site: {site_name}
- Device Name: {device_name}
- Model: {device_model}
- Offline Duration: {offline_duration}

Recommended Troubleshooting Steps:
1. Power Check: Verify the device is receiving power (check the PoE switch port, PoE injector, or mains plug).
2. Cabling: Ensure the uplink Ethernet cable is securely connected and not damaged.
3. Upstream Equipment: Check if the switch port this device connects to is active and configured correctly.
4. Hard Reboot: Try physically unplugging the device, waiting 10 seconds, and plugging it back in.
5. ISP Check: If this device is a Gateway/Router, verify that the ISP modem is online and functioning.

Thanks,
Emerald IT UniFi Monitor
"""
    
    msg = MIMEMultipart()
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    # Retry logic (similar to your PS script)
    for attempt in range(1, 4):
        try:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10)
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
            server.quit()
            log(f"*** EMAIL SENT: {device_name} at {site_name} ***")
            return True
        except Exception as e:
            log(f"!! Email attempt {attempt} failed for {device_name}: {str(e)}")
            time.sleep(5)
            
    return False

def format_duration(diff_sec):
    if diff_sec < 0: return ""
    diff_mins = diff_sec / 60
    if diff_mins < 60: return f"{int(diff_mins)}m"
    diff_hours = diff_mins / 60
    if diff_hours < 24: return f"{int(diff_hours)}h"
    diff_days = diff_hours / 24
    if diff_days < 7: return f"{int(diff_days)}d"
    diff_weeks = diff_days / 7
    if diff_weeks < 4: return f"{int(diff_weeks)}w"
    diff_months = diff_days / 30.44
    if diff_months < 12: return f"{int(diff_months)}mo"
    return f"{int(diff_months/12)}y"

def parse_iso_time(ts_str):
    try:
        if not ts_str: return None
        clean_ts = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def fetch_modern_unifi(alert_state):
    if not API_KEY: return []
    cards = []
    try:
        headers = {"X-API-KEY": API_KEY, "Accept": "application/json"}
        
        dev_res = requests.get(f"{MODERN_URL}/devices", headers=headers, timeout=30).json().get('data', [])
        sites_res = requests.get(f"{MODERN_URL}/sites", headers=headers, timeout=30).json().get('data', [])
        hosts_res = requests.get(f"{MODERN_URL}/hosts", headers=headers, timeout=30).json().get('data', [])
        
        site_health_map = {s.get('hostId'): s for s in sites_res if s.get('hostId')}
        host_map = {h.get('id'): h for h in hosts_res if h.get('id')}

        current_time = datetime.now(timezone.utc)
        current_bucket = int(current_time.timestamp() / 300)

        for host_group in dev_res:
            host_id = host_group.get('hostId')
            name = host_group.get('hostName') or "Unnamed Site"
            devices_list = host_group.get('devices', [])
            
            stats = site_health_map.get(host_id, {}).get('statistics', {})
            counts = stats.get('counts', {})
            isp_name = stats.get('ispInfo', {}).get('name', '')
            host_info = host_map.get(host_id, {})
            
            status = "Green"
            weight = 0
            issues = []
            inventory = []
            primary_model = None 
            offline_devs = 0
            total_devs = len(devices_list)

            for d in devices_list:
                dev_status = str(d.get('status', 'unknown')).lower()
                has_update = d.get('firmwareStatus') == 'updateAvailable'
                dev_mac = d.get('mac')
                dev_name = d.get("name") or dev_mac
                dev_model = d.get("model") or "UniFi Device"
                offline_str = ""

                if d.get('isConsole'):
                    primary_model = dev_model

                if dev_status != "online":
                    offline_devs += 1
                    dt_str = d.get('lastSeenAt') or d.get('lastConnectionStateChange')
                    if not dt_str and d.get('isConsole'):
                        dt_str = host_info.get('lastConnectionStateChange')
                        
                    last_seen_dt = parse_iso_time(dt_str)
                    if last_seen_dt:
                        try:
                            diff_sec = (current_time - last_seen_dt).total_seconds()
                            offline_str = format_duration(diff_sec)
                            
                            # --- EMAIL ALERT LOGIC ---
                            if diff_sec >= ALERT_THRESHOLD_SECONDS and dev_mac not in alert_state:
                                if send_offline_alert(name, dev_name, dev_model, offline_str):
                                    alert_state[dev_mac] = current_time.isoformat()
                                    
                        except: 
                            offline_str = ">30d"
                    else:
                        offline_str = ">30d"
                        # Fallback alert for >30d
                        if dev_mac not in alert_state:
                            if send_offline_alert(name, dev_name, dev_model, "> 30 days"):
                                alert_state[dev_mac] = current_time.isoformat()
                else:
                    # Device is online, clear alert state
                    if dev_mac in alert_state:
                        del alert_state[dev_mac]
                
                inventory.append({
                    "name": dev_name,
                    "model": dev_model,
                    "status": dev_status.upper(),
                    "has_update": has_update,
                    "offline_duration": offline_str
                })

                if d.get('isConsole') and dev_status != "online":
                    status = "Red"; weight = 20
                    time_display = f"{offline_str} ago" if offline_str else "Critical"
                    issues.append({"label": "🚨 GATEWAY OFFLINE", "time": time_display, "severity": "critical"})

            if total_devs > 0 and offline_devs == total_devs and status != "Red":
                status = "Red"; weight = 20
                issues.append({"label": "🚨 SITE COMPLETELY OFFLINE", "time": "Critical", "severity": "critical"})

            if not primary_model and devices_list:
                primary_model = devices_list[0].get('model', 'Gateway')
            elif not primary_model:
                primary_model = "Gateway"

            if status != "Red":
                internet_issues = stats.get('internetIssues', [])
                active_isp = False
                for iss in internet_issues:
                    if (current_bucket - iss.get('index', 0)) <= (ALERT_WINDOW_MINS / 5):
                        active_isp = True; break

                if offline_devs > 0:
                    status = "Yellow"; weight = 10
                    issues.append({"label": f"⚠️ {offline_devs} Device(s) Offline", "time": "Partial", "severity": "warning"})
                
                if active_isp:
                    status = "Yellow"; weight = 5
                    issues.append({"label": "📡 RECENT ISP ISSUE", "time": "< 4h ago", "severity": "warning"})

            cards.append({
                "SiteName": name,
                "Model": primary_model,
                "ISP": isp_name,
                "Inventory": inventory,
                "Status": status,
                "IssuesCount": weight,
                "IssuesList": issues
            })
    except Exception as e:
        log(f"!! Error fetching Modern API: {str(e)}")
    
    return cards

def fetch_classic_unifi(alert_state):
    if not CLASSIC_USER or not CLASSIC_PASS: return []
    cards = []
    current_time = datetime.now(timezone.utc)
    
    try:
        session = requests.Session()
        login_res = session.post(f"{CLASSIC_URL}/api/login", json={"username": CLASSIC_USER, "password": CLASSIC_PASS, "remember": True}, verify=False, timeout=15)
        login_res.raise_for_status()

        sites_res = session.get(f"{CLASSIC_URL}/api/self/sites", verify=False, timeout=15).json().get('data', [])

        for site in sites_res:
            site_name = site.get('name')
            site_desc = site.get('desc', 'Unnamed Site')
            
            dev_res = session.get(f"{CLASSIC_URL}/api/s/{site_name}/stat/device", verify=False, timeout=15).json().get('data', [])
            if not dev_res: continue

            status = "Green"
            weight = 0
            issues = []
            inventory = []
            offline_count = 0
            primary_model = None
            total_devs = len(dev_res)

            for dev in dev_res:
                dev_mac = dev.get("mac")
                d_name = dev.get("name") or dev_mac or "Unknown Device"
                d_model = dev.get("model", "UniFi Device")
                is_offline = (dev.get("state", 0) == 0)
                offline_str = ""

                if dev.get('type') == 'ugw':
                    primary_model = d_model

                if is_offline:
                    offline_count += 1
                    last_seen = dev.get('last_seen') or dev.get('last_disconnect')
                    
                    if last_seen:
                        try:
                            diff_sec = (current_time - datetime.fromtimestamp(float(last_seen), timezone.utc)).total_seconds()
                            offline_str = format_duration(diff_sec)
                            
                            # --- EMAIL ALERT LOGIC ---
                            if diff_sec >= ALERT_THRESHOLD_SECONDS and dev_mac not in alert_state:
                                if send_offline_alert(site_desc, d_name, d_model, offline_str):
                                    alert_state[dev_mac] = current_time.isoformat()
                                    
                        except Exception:
                            offline_str = ">30d"
                    else:
                        offline_str = ">30d"
                        # Fallback alert
                        if dev_mac not in alert_state:
                            if send_offline_alert(site_desc, d_name, d_model, "> 30 days"):
                                alert_state[dev_mac] = current_time.isoformat()

                    if dev.get('type') == 'ugw':
                        status = "Red"; weight = 20
                        time_display = f"{offline_str} ago" if offline_str else "Offline"
                        issues.append({"label": "🚨 GATEWAY OFFLINE", "time": time_display, "severity": "critical"})
                else:
                    # Device is online, clear alert state
                    if dev_mac in alert_state:
                        del alert_state[dev_mac]

                inventory.append({
                    "name": d_name,
                    "model": d_model,
                    "status": "OFFLINE" if is_offline else "ONLINE",
                    "has_update": dev.get("upgradable", False),
                    "offline_duration": offline_str
                })

            if total_devs > 0 and offline_count == total_devs and status != "Red":
                status = "Red"; weight = 20
                issues.append({"label": "🚨 SITE COMPLETELY OFFLINE", "time": "Critical", "severity": "critical"})

            if not primary_model:
                primary_model = "Cloud Hosted"

            if status != "Red":
                if offline_count > 0:
                    status = "Yellow"; weight = 10
                    issues.append({"label": f"⚠️ {offline_count} Device(s) Offline", "time": "Partial", "severity": "warning"})

            cards.append({
                "SiteName": f"{site_desc} (Cloud)",
                "Model": primary_model,
                "ISP": "",
                "Inventory": inventory,
                "Status": status,
                "IssuesCount": weight,
                "IssuesList": issues
            })

        session.post(f"{CLASSIC_URL}/api/logout", verify=False)
    except Exception as e:
        log(f"!! Error fetching Classic API: {str(e)}")

    return cards

def harvest_data():
    # Load previously sent alerts
    alert_state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                alert_state = json.load(f)
        except Exception: pass

    while True:
        log(">>> Starting Unified Multi-Controller Harvest...")
        modern_cards = fetch_modern_unifi(alert_state)
        classic_cards = fetch_classic_unifi(alert_state)
        
        all_cards = modern_cards + classic_cards
        all_cards.sort(key=lambda x: (-x['IssuesCount'], x['SiteName']))
        
        # Save dashboard data
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"timestamp": datetime.now().strftime("%H:%M:%S"), "sites": all_cards}, f, indent=4)
            
        # Save alert state to avoid repeat emails
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(alert_state, f)
            
        log(f"*** HARVEST SUCCESS: Processed {len(modern_cards)} Modern + {len(classic_cards)} Classic sites ***")
        time.sleep(POLL_INTERVAL)

class MyHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args): pass 
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Access-Control-Allow-Origin', '*')
        SimpleHTTPRequestHandler.end_headers(self)

if __name__ == "__main__":
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f: json.dump({"timestamp": "N/A", "sites": []}, f)
    threading.Thread(target=harvest_data, daemon=True).start()
    HTTPServer(('0.0.0.0', 8080), MyHandler).serve_forever()
