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
        log("!! ERROR: UNIFI_API_KEY is missing!")
        return

    headers = {"X-API-KEY": API_KEY, "Accept": "application/json"}

    while True:
        try:
            log(">>> Starting Final Unified UniFi Harvest...")
            
            # 1. Fetch Master Device List (Friendly Names + Inventory)
            dev_res = requests.get(f"{BASE_URL}/devices", headers=headers, timeout=30)
            dev_res.raise_for_status()
            devices_raw = dev_res.json().get('data', [])

            # 2. Fetch Health Statistics (Offline counts + ISP Issues)
            sites_res = requests.get(f"{BASE_URL}/sites", headers=headers, timeout=30)
            sites_res.raise_for_status()
            sites_raw = sites_res.json().get('data', [])

            # Map site health by hostId
            site_health_map = {s.get('hostId'): s for s in sites_raw if s.get('hostId')}

            final_cards = []

            for host_group in devices_raw:
                host_id = host_group.get('hostId')
                name = host_group.get('hostName') or "Unnamed Site"
                devices_list = host_group.get('devices', [])
                
                # Get the health stats for this specific host
                stats = site_health_map.get(host_id, {}).get('statistics', {})
                counts = stats.get('counts', {})
                
                status = "Green"
                weight = 0
                issues = []
                inventory = []

                # Build Inventory and check for Gateway (Red) status
                for d in devices_list:
                    dev_status = str(d.get('status', 'unknown')).lower()
                    inventory.append({
                        "name": d.get("name") or d.get("mac"),
                        "model": d.get("model") or "UniFi Device",
                        "status": dev_status.upper()
                    })

                    # 🔴 CRITICAL: Main Gateway/Console is Offline
                    if d.get('isConsole') and dev_status != "online":
                        status = "Red"
                        weight = 20
                        issues.append({"label": "🚨 GATEWAY OFFLINE", "time": "Critical", "severity": "critical"})

                # 🟡 WARNING: Check for sub-device outages or ISP issues if not already Red
                if status != "Red":
                    offline_count = counts.get('offlineDevice', 0)
                    critical_notifs = counts.get('criticalNotification', 0)
                    
                    # check for internet issues in statistics
                    internet_issues = stats.get('internetIssues', [])
                    wan_data = stats.get('wans', {}).get('WAN', {})
                    wan_issues = wan_data.get('wanIssues', [])

                    if offline_count > 0:
                        status = "Yellow"
                        weight = 10
                        issues.append({"label": f"⚠️ {offline_count} Device(s) Offline", "time": "Partial Outage", "severity": "warning"})
                    
                    if internet_issues or wan_issues:
                        status = "Yellow"
                        if weight < 5: weight = 5
                        issues.append({"label": "📡 ISP/Latency Issues", "time": "Check ISP", "severity": "warning"})

                    if critical_notifs > 0:
                        status = "Yellow"
                        if weight < 8: weight = 8
                        issues.append({"label": f"🔔 {critical_notifs} System Alert(s)", "time": "Action Required", "severity": "warning"})

                final_cards.append({
                    "SiteName": name,
                    "Inventory": inventory,
                    "Status": status,
                    "IssuesCount": weight,
                    "IssuesList": issues
                })

            # Sort: Priority (Offline > Warning > Healthy) then Alphabetical
            final_cards.sort(key=lambda x: (-x['IssuesCount'], x['SiteName']))
            
            payload = {
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "sites": final_cards
            }
            
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4)
            log(f"*** HARVEST SUCCESS: Processed {len(final_cards)} sites ***")
            
        except Exception as e:
            log(f"!! ERROR: {str(e)}")
            traceback.print_exc()
        
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
