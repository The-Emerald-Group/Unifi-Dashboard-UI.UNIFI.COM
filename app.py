import os
import requests
import json
import time
import threading
import traceback
import urllib3
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, HTTPServer

# Suppress insecure HTTPS warnings for the classic controller
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIGURATION ---
# Modern API (UI.com)
API_KEY = os.environ.get("UNIFI_API_KEY")
MODERN_URL = "https://api.ui.com/v1"

# Classic API (Cloud Controller)
CLASSIC_URL = os.environ.get("CLASSIC_URL", "https://emerald.unificloud.co.uk:8443")
CLASSIC_USER = os.environ.get("CLASSIC_USER")
CLASSIC_PASS = os.environ.get("CLASSIC_PASS")

DATA_FILE = "data.json"
POLL_INTERVAL = 300 
ALERT_WINDOW_MINS = 240  # 4 hours for modern ISP issues

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def fetch_modern_unifi():
    if not API_KEY:
        log("!! Skipping Modern API: UNIFI_API_KEY missing.")
        return []
        
    cards = []
    try:
        headers = {"X-API-KEY": API_KEY, "Accept": "application/json"}
        
        dev_res = requests.get(f"{MODERN_URL}/devices", headers=headers, timeout=30)
        dev_res.raise_for_status()
        devices_raw = dev_res.json().get('data', [])

        sites_res = requests.get(f"{MODERN_URL}/sites", headers=headers, timeout=30)
        sites_res.raise_for_status()
        sites_raw = sites_res.json().get('data', [])
        site_health_map = {s.get('hostId'): s for s in sites_raw if s.get('hostId')}

        current_time = datetime.now(timezone.utc)
        current_bucket = int(current_time.timestamp() / 300)

        for host_group in devices_raw:
            host_id = host_group.get('hostId')
            name = host_group.get('hostName') or "Unnamed Site"
            devices_list = host_group.get('devices', [])
            
            stats = site_health_map.get(host_id, {}).get('statistics', {})
            counts = stats.get('counts', {})
            isp_name = stats.get('ispInfo', {}).get('name', '')
            
            status = "Green"
            weight = 0
            issues = []
            inventory = []

            for d in devices_list:
                dev_status = str(d.get('status', 'unknown')).lower()
                has_update = d.get('firmwareStatus') == 'updateAvailable'
                
                inventory.append({
                    "name": d.get("name") or d.get("mac"),
                    "model": d.get("model") or "UniFi Device",
                    "status": dev_status.upper(),
                    "has_update": has_update
                })

                if d.get('isConsole') and dev_status != "online":
                    status = "Red"; weight = 20
                    issues.append({"label": "🚨 GATEWAY OFFLINE", "time": "Critical", "severity": "critical"})

            if status != "Red":
                internet_issues = stats.get('internetIssues', [])
                active_isp = False
                for iss in internet_issues:
                    if (current_bucket - iss.get('index', 0)) <= 48:
                        active_isp = True
                        break

                offline_count = counts.get('offlineDevice', 0)
                if offline_count > 0:
                    status = "Yellow"; weight = 10
                    issues.append({"label": f"⚠️ {offline_count} Device(s) Offline", "time": "Partial", "severity": "warning"})
                
                if active_isp:
                    status = "Yellow"; weight = 5
                    issues.append({"label": "📡 RECENT ISP ISSUE", "time": "< 4h ago", "severity": "warning"})

            cards.append({
                "SiteName": name,
                "Model": devices_list[0].get('model') if devices_list else 'Gateway',
                "ISP": isp_name,
                "Inventory": inventory,
                "Status": status,
                "IssuesCount": weight,
                "IssuesList": issues
            })
    except Exception as e:
        log(f"!! Error fetching Modern API: {str(e)}")
    
    return cards

def fetch_classic_unifi():
    if not CLASSIC_USER or not CLASSIC_PASS:
        log("!! Skipping Classic API: Credentials missing.")
        return []
        
    cards = []
    current_time = datetime.now(timezone.utc)
    
    try:
        session = requests.Session()
        login_res = session.post(f"{CLASSIC_URL}/api/login", json={"username": CLASSIC_USER, "password": CLASSIC_PASS, "remember": True}, verify=False, timeout=15)
        login_res.raise_for_status()

        sites_res = session.get(f"{CLASSIC_URL}/api/self/sites", verify=False, timeout=15)
        sites_res.raise_for_status()
        sites = sites_res.json().get('data', [])

        for site in sites:
            site_name = site.get('name')
            site_desc = site.get('desc', 'Unnamed Site')
            
            dev_res = session.get(f"{CLASSIC_URL}/api/s/{site_name}/stat/device", verify=False, timeout=15)
            devices = dev_res.json().get('data', [])

            if not devices: continue

            status = "Green"
            weight = 0
            issues = []
            inventory = []
            offline_count = 0

            for dev in devices:
                d_name = dev.get("name") or dev.get("mac", "Unknown Device")
                d_model = dev.get("model", "UniFi Device")
                is_offline = (dev.get("state", 0) == 0)
                
                inventory.append({
                    "name": d_name,
                    "model": d_model,
                    "status": "OFFLINE" if is_offline else "ONLINE",
                    "has_update": dev.get("upgradable", False)
                })

                if is_offline:
                    offline_count += 1
                    time_display = "Offline"
                    last_seen = dev.get('last_seen')
                    if last_seen:
                        diff_mins = (current_time - datetime.fromtimestamp(last_seen, timezone.utc)).total_seconds() / 60
                        if diff_mins > 2880: time_display = f"{int(diff_mins / 1440)}d ago"
                        else:
                            h, m = divmod(int(diff_mins), 60)
                            time_display = f"{h}h {m}m ago" if h > 0 else f"{m}m ago"

                    if dev.get('type') == 'ugw':
                        status = "Red"; weight = 20
                        issues.append({"label": f"🚨 GATEWAY OFFLINE", "time": time_display, "severity": "critical"})

            if offline_count > 0 and status != "Red":
                status = "Yellow"; weight = 10
                issues.append({"label": f"⚠️ {offline_count} Device(s) Offline", "time": "Partial", "severity": "warning"})

            cards.append({
                "SiteName": f"{site_desc} (Cloud)",
                "Model": "Cloud Hosted",
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
    while True:
        log(">>> Starting Unified Multi-Controller Harvest...")
        
        # Run both harvesters
        modern_cards = fetch_modern_unifi()
        classic_cards = fetch_classic_unifi()
        
        # Combine the lists
        all_cards = modern_cards + classic_cards
        
        # Sort combined list: Highest severity first, then alphabetical
        all_cards.sort(key=lambda x: (-x['IssuesCount'], x['SiteName']))
        
        payload = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "sites": all_cards
        }
        
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
            
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
