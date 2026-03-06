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

# --- CONFIGURATION (PULLED FROM DOCKER COMPOSE) ---
API_KEY = os.environ.get("UNIFI_API_KEY")
MODERN_URL = "https://api.ui.com/v1"

CLASSIC_URL = os.environ.get("CLASSIC_URL")
CLASSIC_USER = os.environ.get("CLASSIC_USER")
CLASSIC_PASS = os.environ.get("CLASSIC_PASS")

# --- EXCLUSIONS ---
IGNORE_SITES_RAW = os.environ.get("IGNORE_SITES", "")
# Create a clean, lowercase list of sites to ignore
IGNORE_SITES = [s.strip().lower() for s in IGNORE_SITES_RAW.split(",") if s.strip()]

# --- SMTP EMAIL CONFIGURATION ---
SMTP_SERVER = os.environ.get("SMTP_SERVER")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 25))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_TO = os.environ.get("EMAIL_TO")
ALERT_THRESHOLD_SECONDS = 8 * 3600  # 8 Hours

# --- DATA STORAGE PATHS ---
DATA_FILE = "data.json"
TEMP_DATA_FILE = "data.tmp.json" 

# --- PERMANENT DOCKER VOLUME STORAGE (For alerts only) ---
DATA_DIR = "/unifi_data"
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = f"{DATA_DIR}/alerts_v2.json" 
TEMP_STATE_FILE = f"{DATA_DIR}/alerts_v2.tmp.json"
# ---------------------------------------------------------

POLL_INTERVAL = 300 
ALERT_WINDOW_MINS = 240 
HISTORICAL_OFFLINE_SECONDS = 2592000  # 30 Days

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def send_consolidated_offline_alert(site_name, devices):
    count = len(devices)
    s_plural = "s" if count > 1 else ""
    is_are = "are" if count > 1 else "is"
    
    subject = f"🚨 URGENT: {count} UniFi Device{s_plural} Offline - {site_name}"
    
    table_rows = ""
    for d in devices:
        table_rows += f"""
        <tr>
          <td style="padding: 10px 15px; border-bottom: 1px solid #eaeaea; font-weight: bold; color: #222;">{d['name']}</td>
          <td style="padding: 10px 15px; border-bottom: 1px solid #eaeaea; color: #666;">{d['model']}</td>
          <td style="padding: 10px 15px; border-bottom: 1px solid #eaeaea; color: #ff3b30; font-weight: bold;">{d['duration']}</td>
        </tr>
        """
    
    html_body = f"""
    <html>
      <body style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f5f7; margin: 0; padding: 30px 10px;">
        <div style="max-width: 650px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.05);">
          <div style="background-color: #ff3b30; color: #ffffff; padding: 20px; text-align: center;">
            <h2 style="margin: 0; font-size: 22px; letter-spacing: 1px;">🚨 DEVICE{s_plural.upper()} OFFLINE ALERT</h2>
          </div>
          <div style="padding: 30px;">
            <p style="font-size: 16px; color: #444; line-height: 1.5; margin-top: 0;">
              An automated alert has been triggered by the Emerald IT UniFi Monitor. <b>{count} network device{s_plural}</b> at <strong>{site_name}</strong> {is_are} unreachable for over 8 hours.
            </p>
            <table style="width: 100%; border-collapse: collapse; margin-top: 20px; margin-bottom: 25px; background-color: #f9f9f9; border-radius: 6px; overflow: hidden; text-align: left;">
              <tr style="background-color: #eaeaea;">
                <th style="padding: 12px 15px; color: #444; font-size: 14px;">Device Name</th>
                <th style="padding: 12px 15px; color: #444; font-size: 14px;">Hardware Model</th>
                <th style="padding: 12px 15px; color: #444; font-size: 14px;">Time Offline</th>
              </tr>
              {table_rows}
            </table>
            <h3 style="color: #222; font-size: 16px; margin-bottom: 10px; border-bottom: 2px solid #00A1E4; display: inline-block; padding-bottom: 5px;">Recommended Troubleshooting</h3>
            <ul style="color: #555; line-height: 1.6; padding-left: 20px; font-size: 14px;">
              <li style="margin-bottom: 6px;"><b>Power Check:</b> Verify the device is receiving power.</li>
              <li style="margin-bottom: 6px;"><b>Cabling:</b> Ensure the uplink Ethernet cable is securely connected.</li>
              <li style="margin-bottom: 6px;"><b>Upstream Equipment:</b> Check if the switch port this device connects to is active.</li>
              <li style="margin-bottom: 6px;"><b>Hard Reboot:</b> Try physically unplugging the device and plugging it back in.</li>
              <li style="margin-bottom: 6px;"><b>ISP Check:</b> Verify that the ISP modem is online.</li>
            </ul>
          </div>
          <div style="background-color: #f1f1f1; padding: 15px; text-align: center; color: #888; font-size: 12px; border-top: 1px solid #eaeaea;">
            <strong>Emerald IT</strong> • Automated Network Monitoring System
          </div>
        </div>
      </body>
    </html>
    """
    return send_email(subject, html_body, site_name)

def send_consolidated_recovery_alert(site_name, devices):
    count = len(devices)
    s_plural = "s" if count > 1 else ""
    subject = f"✅ RECOVERED: {count} UniFi Device{s_plural} Online - {site_name}"
    
    table_rows = ""
    for d in devices:
        table_rows += f"""
        <tr>
          <td style="padding: 10px 15px; border-bottom: 1px solid #eaeaea; font-weight: bold; color: #222;">{d['name']}</td>
          <td style="padding: 10px 15px; border-bottom: 1px solid #eaeaea; color: #666;">{d['model']}</td>
          <td style="padding: 10px 15px; border-bottom: 1px solid #eaeaea; color: #4cd964; font-weight: bold;">ONLINE</td>
        </tr>
        """

    html_body = f"""
    <html>
      <body style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f5f7; margin: 0; padding: 30px 10px;">
        <div style="max-width: 650px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.05);">
          <div style="background-color: #4cd964; color: #ffffff; padding: 20px; text-align: center;">
            <h2 style="margin: 0; font-size: 22px; letter-spacing: 1px;">✅ DEVICE{s_plural.upper()} RECOVERED</h2>
          </div>
          <div style="padding: 30px;">
            <p style="font-size: 16px; color: #444; line-height: 1.5; margin-top: 0;">
              Good news! <b>{count} network device{s_plural}</b> at <strong>{site_name}</strong> that previously triggered an alert have reconnected to the controller and are functioning normally.
            </p>
            <table style="width: 100%; border-collapse: collapse; margin-top: 20px; margin-bottom: 25px; background-color: #f9f9f9; border-radius: 6px; overflow: hidden; text-align: left;">
              <tr style="background-color: #eaeaea;">
                <th style="padding: 12px 15px; color: #444; font-size: 14px;">Device Name</th>
                <th style="padding: 12px 15px; color: #444; font-size: 14px;">Hardware Model</th>
                <th style="padding: 12px 15px; color: #444; font-size: 14px;">Current Status</th>
              </tr>
              {table_rows}
            </table>
          </div>
          <div style="background-color: #f1f1f1; padding: 15px; text-align: center; color: #888; font-size: 12px; border-top: 1px solid #eaeaea;">
            <strong>Emerald IT</strong> • Automated Network Monitoring System
          </div>
        </div>
      </body>
    </html>
    """
    return send_email(subject, html_body, site_name)

def send_email(subject, html_body, log_identifier):
    if not SMTP_SERVER or not EMAIL_TO:
        return False
        
    msg = MIMEMultipart('alternative')
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO
    msg['Subject'] = subject
    msg.attach(MIMEText(html_body, 'html'))

    for attempt in range(1, 4):
        try:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10)
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
            log(f"*** EMAIL SENT for {log_identifier}: {subject} ***")
            try: server.quit() 
            except Exception: pass
            return True
        except Exception as e:
            log(f"!! Email attempt {attempt} failed for {log_identifier}: {str(e)}")
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

def fetch_modern_unifi(alert_state, pending_offline, pending_recovery):
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
            
            if name.lower() in IGNORE_SITES:
                continue
                
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
            historical_devs = 0
            recent_devs = 0
            total_devs = len(devices_list)

            for d in devices_list:
                dev_status = str(d.get('status', 'unknown')).lower()
                has_update = d.get('firmwareStatus') == 'updateAvailable'
                dev_mac = d.get('mac')
                dev_name = d.get("name") or dev_mac or "Unknown Device"
                dev_model = d.get("model") or "UniFi Device"
                alert_key = dev_mac or f"{name}_{dev_name}"
                offline_str = ""
                is_historical = False

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
                            if diff_sec >= HISTORICAL_OFFLINE_SECONDS:
                                is_historical = True
                            
                            if diff_sec >= ALERT_THRESHOLD_SECONDS and alert_key not in alert_state:
                                if name not in pending_offline: pending_offline[name] = []
                                pending_offline[name].append({"name": dev_name, "model": dev_model, "duration": offline_str, "mac": alert_key})
                        except Exception: 
                            offline_str = ">30d"
                            is_historical = True
                            if alert_key not in alert_state:
                                if name not in pending_offline: pending_offline[name] = []
                                pending_offline[name].append({"name": dev_name, "model": dev_model, "duration": "> 30 days", "mac": alert_key})
                    else:
                        offline_str = ">30d"
                        is_historical = True
                        if alert_key not in alert_state:
                            if name not in pending_offline: pending_offline[name] = []
                            pending_offline[name].append({"name": dev_name, "model": dev_model, "duration": "> 30 days", "mac": alert_key})
                            
                    if is_historical:
                        historical_devs += 1
                    else:
                        recent_devs += 1
                        
                else:
                    if alert_key in alert_state:
                        if name not in pending_recovery: pending_recovery[name] = []
                        pending_recovery[name].append({"name": dev_name, "model": dev_model, "mac": alert_key})
                
                inventory.append({
                    "name": dev_name,
                    "model": dev_model,
                    "status": dev_status.upper(),
                    "has_update": has_update,
                    "offline_duration": offline_str
                })

                if d.get('isConsole') and dev_status != "online":
                    if is_historical:
                        if status != "Red": 
                            status = "Grey"; weight = 5
                        issues.append({"label": "💤 GATEWAY HISTORICALLY DOWN", "time": "> 30d", "severity": "historical"})
                    else:
                        status = "Red"; weight = 20
                        time_display = f"{offline_str} ago" if offline_str else "Critical"
                        issues.append({"label": "🚨 GATEWAY OFFLINE", "time": time_display, "severity": "critical"})

            if total_devs > 0 and offline_devs == total_devs and status != "Red":
                if offline_devs == historical_devs:
                    status = "Grey"; weight = 5
                    issues.append({"label": "💤 SITE HISTORICALLY OFFLINE", "time": "> 30d", "severity": "historical"})
                else:
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

                if active_isp:
                    status = "Yellow"; weight = 15
                    issues.append({"label": "📡 RECENT ISP ISSUE", "time": "< 4h ago", "severity": "warning"})
                    
                if recent_devs > 0:
                    if weight < 10: 
                        status = "Yellow"; weight = 10
                    issues.append({"label": f"⚠️ {recent_devs} Device(s) Offline", "time": "Partial", "severity": "warning"})
                    
                if historical_devs > 0 and not any(i['severity'] == 'historical' for i in issues):
                    if weight < 5: 
                        status = "Green"; weight = 5 # <-- Allows the card to stay green
                    issues.append({"label": f"💤 {historical_devs} Device(s) Historically Down", "time": "> 30d", "severity": "historical"})

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

def fetch_classic_unifi(alert_state, pending_offline, pending_recovery):
    if not CLASSIC_URL or not CLASSIC_USER or not CLASSIC_PASS: 
        return []
        
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
            display_name = f"{site_desc} (Cloud)"
            
            if site_desc.lower() in IGNORE_SITES or display_name.lower() in IGNORE_SITES:
                continue
            
            dev_res = session.get(f"{CLASSIC_URL}/api/s/{site_name}/stat/device", verify=False, timeout=15).json().get('data', [])
            if not dev_res: continue

            status = "Green"
            weight = 0
            issues = []
            inventory = []
            offline_count = 0
            historical_devs = 0
            recent_devs = 0
            primary_model = None
            total_devs = len(dev_res)

            for dev in dev_res:
                dev_mac = dev.get("mac")
                d_name = dev.get("name") or dev_mac or "Unknown Device"
                d_model = dev.get("model", "UniFi Device")
                alert_key = dev_mac or f"{site_desc}_{d_name}"
                is_offline = (dev.get("state", 0) == 0)
                offline_str = ""
                is_historical = False

                if dev.get('type') == 'ugw':
                    primary_model = d_model

                if is_offline:
                    offline_count += 1
                    last_seen = dev.get('last_seen') or dev.get('last_disconnect')
                    
                    if last_seen:
                        try:
                            diff_sec = (current_time - datetime.fromtimestamp(float(last_seen), timezone.utc)).total_seconds()
                            offline_str = format_duration(diff_sec)
                            if diff_sec >= HISTORICAL_OFFLINE_SECONDS:
                                is_historical = True
                                
                            if diff_sec >= ALERT_THRESHOLD_SECONDS and alert_key not in alert_state:
                                if site_desc not in pending_offline: pending_offline[site_desc] = []
                                pending_offline[site_desc].append({"name": d_name, "model": d_model, "duration": offline_str, "mac": alert_key})
                        except Exception:
                            offline_str = ">30d"
                            is_historical = True
                            if alert_key not in alert_state:
                                if site_desc not in pending_offline: pending_offline[site_desc] = []
                                pending_offline[site_desc].append({"name": d_name, "model": d_model, "duration": "> 30 days", "mac": alert_key})
                    else:
                        offline_str = ">30d"
                        is_historical = True
                        if alert_key not in alert_state:
                            if site_desc not in pending_offline: pending_offline[site_desc] = []
                            pending_offline[site_desc].append({"name": d_name, "model": d_model, "duration": "> 30 days", "mac": alert_key})

                    if is_historical:
                        historical_devs += 1
                    else:
                        recent_devs += 1

                    if dev.get('type') == 'ugw':
                        if is_historical:
                            if status != "Red": 
                                status = "Grey"; weight = 5
                            issues.append({"label": "💤 GATEWAY HISTORICALLY DOWN", "time": "> 30d", "severity": "historical"})
                        else:
                            status = "Red"; weight = 20
                            time_display = f"{offline_str} ago" if offline_str else "Offline"
                            issues.append({"label": "🚨 GATEWAY OFFLINE", "time": time_display, "severity": "critical"})
                else:
                    if alert_key in alert_state:
                        if site_desc not in pending_recovery: pending_recovery[site_desc] = []
                        pending_recovery[site_desc].append({"name": d_name, "model": d_model, "mac": alert_key})

                inventory.append({
                    "name": d_name,
                    "model": d_model,
                    "status": "OFFLINE" if is_offline else "ONLINE",
                    "has_update": dev.get("upgradable", False),
                    "offline_duration": offline_str
                })

            if total_devs > 0 and offline_count == total_devs and status != "Red":
                if offline_count == historical_devs:
                    status = "Grey"; weight = 5
                    issues.append({"label": "💤 SITE HISTORICALLY OFFLINE", "time": "> 30d", "severity": "historical"})
                else:
                    status = "Red"; weight = 20
                    issues.append({"label": "🚨 SITE COMPLETELY OFFLINE", "time": "Critical", "severity": "critical"})

            if not primary_model:
                primary_model = "Cloud Hosted"

            if status != "Red":
                if recent_devs > 0:
                    if weight < 10: 
                        status = "Yellow"; weight = 10
                    issues.append({"label": f"⚠️ {recent_devs} Device(s) Offline", "time": "Partial", "severity": "warning"})
                    
                if historical_devs > 0 and not any(i['severity'] == 'historical' for i in issues):
                    if weight < 5: 
                        status = "Green"; weight = 5 # <-- Allows the card to stay green
                    issues.append({"label": f"💤 {historical_devs} Device(s) Historically Down", "time": "> 30d", "severity": "historical"})

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
    alert_state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: alert_state = json.load(f)
        except Exception: pass

    while True:
        log(">>> Starting Unified Multi-Controller Harvest...")
        
        pending_offline = {}
        pending_recovery = {}

        modern_cards = fetch_modern_unifi(alert_state, pending_offline, pending_recovery)
        classic_cards = fetch_classic_unifi(alert_state, pending_offline, pending_recovery)
        
        total_offline_queued = sum(len(v) for v in pending_offline.values())
        if total_offline_queued > 0:
            log(f"--> Queueing OFFLINE alerts for {total_offline_queued} devices across {len(pending_offline)} sites.")
            
        for site, devices in pending_offline.items():
            if send_consolidated_offline_alert(site, devices):
                for d in devices:
                    alert_state[d['mac']] = datetime.now(timezone.utc).isoformat()

        for site, devices in pending_recovery.items():
            if send_consolidated_recovery_alert(site, devices):
                for d in devices:
                    if d['mac'] in alert_state:
                        del alert_state[d['mac']]

        all_cards = modern_cards + classic_cards
        all_cards.sort(key=lambda x: (-x['IssuesCount'], x['SiteName']))
        
        with open(TEMP_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"timestamp": datetime.now().strftime("%H:%M:%S"), "sites": all_cards}, f, indent=4)
        
        os.replace(TEMP_DATA_FILE, DATA_FILE)
            
        with open(TEMP_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(alert_state, f)
            
        os.replace(TEMP_STATE_FILE, STATE_FILE)
            
        log(f"*** HARVEST SUCCESS: Processed {len(modern_cards)} Modern + {len(classic_cards)} Classic sites ***")
        time.sleep(POLL_INTERVAL)

class MyHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args): pass 
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Access-Control-Allow-Origin', '*')
        SimpleHTTPRequestHandler.end_headers(self)

if __name__ == "__main__":
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
        
    if not os.path.exists(DATA_FILE):
        with open(TEMP_DATA_FILE, "w") as f: 
            json.dump({"timestamp": "N/A", "sites": []}, f)
        os.replace(TEMP_DATA_FILE, DATA_FILE)
        
    threading.Thread(target=harvest_data, daemon=True).start()
    
    HTTPServer(('0.0.0.0', 8080), MyHandler).serve_forever()
