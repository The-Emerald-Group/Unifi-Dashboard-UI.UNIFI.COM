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
API_KEY = os.environ.get("UNIFI_API_KEY")
MODERN_URL = "https://api.ui.com/v1"

CLASSIC_URL = os.environ.get("CLASSIC_URL", "https://emerald.unificloud.co.uk:8443")
CLASSIC_USER = os.environ.get("CLASSIC_USER")
CLASSIC_PASS = os.environ.get("CLASSIC_PASS")

DATA_FILE = "data.json"
POLL_INTERVAL = 300 
ALERT_WINDOW_MINS = 240 

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def format_duration(diff_sec):
    """Progressively format time into m, h, d, w, mo, y"""
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
    """Bulletproof datetime parsing to prevent timezone crashes"""
    try:
        if not ts_str: return None
        clean_ts = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def fetch_modern_unifi():
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

            for d in devices_list:
                dev_status = str(d.get('status', 'unknown')).lower()
                has_update = d.get('firmwareStatus') == 'updateAvailable'
                offline_str = ""

                if d.get('isConsole'):
                    primary_model = d.get('model')

                if dev_status != "online":
                    dt_str = d.get('lastSeenAt') or d.get('lastConnectionStateChange')
                    if not dt_str and d.get('isConsole'):
                        dt_str = host_info.get('lastConnectionStateChange')
                        
                    last_seen_dt = parse_iso_time(dt_str)
                    if last_seen_dt:
                        try:
                            diff_sec = (current_time - last_seen_dt).total_seconds()
                            offline_str = format_duration(diff_sec)
                        except: 
                            offline_str = ">30d"
                    else:
                        # FALLBACK: If API has completely purged the timestamp
                        offline_str = ">30d"
                
                inventory.append({
                    "name": d.get("name") or d.get("mac"),
                    "model": d.get("model") or "UniFi Device",
                    "status": dev_status.upper(),
                    "has_update": has_update,
                    "offline_duration": offline_str
                })

                if d.get('isConsole') and dev_status != "online":
                    status = "Red"; weight = 20
                    time_display = f"{offline_str} ago" if offline_str else "Critical"
                    issues.append({"label": "🚨 GATEWAY OFFLINE", "time": time_display, "severity": "critical"})

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

                offline_count = counts.get('offlineDevice', 0)
                if offline_count > 0:
                    status = "Yellow"; weight = 10
                    issues.append({"label": f"⚠️ {offline_count} Device(s) Offline", "time": "Partial", "severity": "warning"})
                
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

def fetch_classic_unifi():
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

            for dev in dev_res:
                d_name = dev.get("name") or dev.get("mac", "Unknown Device")
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
                        except Exception as time_err:
                            log(f"Time parsing warning for {d_name}: {time_err}")
                            offline_str = ">30d"
                    else:
                        # FALLBACK: If API has completely purged the timestamp
                        offline_str = ">30d"

                    if dev.get('type') == 'ugw':
                        status = "Red"; weight = 20
                        time_display = f"{offline_str} ago" if offline_str else "Offline"
                        issues.append({"label": "🚨 GATEWAY OFFLINE", "time": time_display, "severity": "critical"})

                inventory.append({
                    "name": d_name,
                    "model": d_model,
                    "status": "OFFLINE" if is_offline else "ONLINE",
                    "has_update": dev.get("upgradable", False),
                    "offline_duration": offline_str
                })

            if not primary_model:
                primary_model = "Cloud Hosted"

            if offline_count > 0 and status != "Red":
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
    while True:
        log(">>> Starting Unified Multi-Controller Harvest...")
        modern_cards = fetch_modern_unifi()
        classic_cards = fetch_classic_unifi()
        all_cards = modern_cards + classic_cards
        all_cards.sort(key=lambda x: (-x['IssuesCount'], x['SiteName']))
        
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"timestamp": datetime.now().strftime("%H:%M:%S"), "sites": all_cards}, f, indent=4)
            
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
