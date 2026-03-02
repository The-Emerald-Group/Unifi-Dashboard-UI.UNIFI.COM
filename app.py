import os
import requests
import json
import time
import threading
import traceback
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, HTTPServer

# --- CONFIGURATION ---
API_KEY = os.environ.get("UNIFI_API_KEY")
BASE_URL = "https://api.ui.com/v1"
DATA_FILE = "data.json"
POLL_INTERVAL = 300 

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def harvest_data():
    if not API_KEY:
        log("!! ERROR: UNIFI_API_KEY environment variable is missing!")
        return

    headers = {"X-API-KEY": API_KEY, "Accept": "application/json"}

    while True:
        try:
            log(">>> Starting Combined UniFi V1 Harvest...")
            
            # 1. Fetch Friendly Site Names
            sites_res = requests.get(f"{BASE_URL}/sites", headers=headers, timeout=30)
            sites_res.raise_for_status()
            sites_data = sites_res.json().get('data', [])

            # 2. Fetch Device Hardware
            dev_res = requests.get(f"{BASE_URL}/devices", headers=headers, timeout=30)
            dev_res.raise_for_status()
            devices_data = dev_res.json().get('data', [])

            # Group hardware by Site ID
            inventory_map = {}
            for d in devices_data:
                sid = d.get('siteId')
                if sid:
                    if sid not in inventory_map: inventory_map[sid] = []
                    inventory_map[sid].append({
                        "name": d.get("name") or d.get("mac"),
                        "model": d.get("productName") or "UniFi Device",
                        "status": str(d.get("status", "")).upper()
                    })

            final_cards = []
            current_time = datetime.now(timezone.utc)

            for site in sites_data:
                site_id = site.get('id')
                name = site.get('name') or "Unnamed Site"
                site_inventory = inventory_map.get(site_id, [])
                
                status = "Green"
                weight = 0
                issues = []
                
                # Check for Site-Level Offline Status
                last_seen_str = site.get('reportedAt')
                time_display = ""
                if last_seen_str:
                    try:
                        clean_ts = last_seen_str.replace("Z", "+00:00")
                        last_seen = datetime.fromisoformat(clean_ts)
                        diff_mins = (current_time - last_seen).total_seconds() / 60
                        
                        if diff_mins > 2880: time_display = f"{int(diff_mins / 1440)}d ago"
                        else:
                            h, m = divmod(int(diff_mins), 60)
                            time_display = f"{h}h {m}m ago" if h > 0 else f"{m}m ago"
                        
                        # If site hasn't checked in for 15 mins, mark Red
                        if diff_mins > 15:
                            status = "Red"
                            weight = 10 if diff_mins < 1440 else 5
                            issues.append({"label": "🚨 SITE OFFLINE", "time": time_display, "severity": "critical"})
                    except: pass

                # Check for Health Alerts
                alerts = site.get('statistics', {}).get('alerts', 0)
                if alerts > 0 and status != "Red":
                    status = "Yellow"
                    weight = 2
                    issues.append({"label": f"⚠️ {alerts} Active Alerts", "time": "Active", "severity": "warning"})

                final_cards.append({
                    "SiteName": name,
                    "Inventory": site_inventory,
                    "Status": status,
                    "IssuesCount": weight,
                    "IssuesList": issues
                })

            # Sort: Problems first, then alphabetical
            final_cards.sort(key=lambda x: (-x['IssuesCount'], x['SiteName']))
            
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"timestamp": datetime.now().strftime("%H:%M:%S"), "sites": final_cards}, f, indent=4)
            log(f"*** SUCCESS: Processed {len(final_cards)} sites ***")
            
        except Exception as e:
            log(f"!! ERROR: {str(e)}")
        
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
