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
BASE_URL = "https://api.ui.com"
DATA_FILE = "data.json"
POLL_INTERVAL = 300  # 5 minutes

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def harvest_data():
    if not API_KEY:
        log("!! ERROR: UNIFI_API_KEY environment variable is missing!")
        return

    headers = {
        "X-API-KEY": API_KEY,
        "Accept": "application/json"
    }

    while True:
        try:
            log(">>> Starting UniFi API Harvest...")
            
            # Fetch gateways/consoles
            res = requests.get(f"{BASE_URL}/ea/devices", headers=headers, timeout=30)
            res.raise_for_status()
            devices_data = res.json().get('data', [])

            wallboard_data = {}

            for dev in devices_data:
                # The Gateway IS the site/customer. Let's get its name.
                name = dev.get("name") or dev.get("hostName") or dev.get("mac") or "Unnamed Gateway"
                
                # Try to grab the hardware model (e.g., UDM-Pro, Cloud Gateway Ultra)
                model = dev.get("productName") or dev.get("hardwareName") or dev.get("hardwareId") or "UniFi Gateway"
                
                # Each gateway gets its own card
                if name not in wallboard_data:
                    wallboard_data[name] = {
                        "DeviceName": name, 
                        "Model": model,
                        "Status": "Green", 
                        "IssuesCount": 0, 
                        "IssuesList": []
                    }
                
                # UniFi often uses 'status' or 'state'
                state = str(dev.get("status", dev.get("state", "UNKNOWN"))).upper()
                
                # Logic for Red/Yellow/Green
                if state in ["OFFLINE", "DISCONNECTED", "ADOPTION_FAILED"]:
                    wallboard_data[name]["Status"] = "Red"
                    wallboard_data[name]["IssuesCount"] += 2
                    wallboard_data[name]["IssuesList"].append({
                        "label": f"🚨 OFFLINE",
                        "severity": "critical"
                    })
                elif state in ["UPDATING", "PROVISIONING", "PENDING", "DEGRADED"]:
                    if wallboard_data[name]["Status"] != "Red":
                        wallboard_data[name]["Status"] = "Yellow"
                    
                    wallboard_data[name]["IssuesCount"] += 1
                    wallboard_data[name]["IssuesList"].append({
                        "label": f"⏳ {state}",
                        "severity": "warning"
                    })

            # Sort: Offline Gateways at the top, then alphabetically by Name
            final_output = sorted(wallboard_data.values(), key=lambda x: (-x['IssuesCount'], x['DeviceName']))
            
            payload = {
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "devices": final_output
            }
            
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4)
            log("*** HARVEST SUCCESS ***")
            
        except Exception as e:
            log(f"!! ERROR: {str(e)}")
            log(traceback.format_exc())
        
        time.sleep(POLL_INTERVAL)

class MyHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args): 
        pass 
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Access-Control-Allow-Origin', '*')
        SimpleHTTPRequestHandler.end_headers(self)

if __name__ == "__main__":
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f: 
            json.dump({"timestamp": "N/A", "devices": []}, f)
    
    threading.Thread(target=harvest_data, daemon=True).start()
    print("Web Server starting on port 8080...")
    HTTPServer(('0.0.0.0', 8080), MyHandler).serve_forever()
