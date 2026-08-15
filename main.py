import os, json, logging, requests, psycopg2, stripe, urllib3, time
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Disable SSL warnings for internal council certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="Vector Data Labs - V22.0 Discovery Master", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# --- ENVIRONMENT VARIABLES ---
OKEY, SURL = os.getenv("OPENAI_API_KEY"), os.getenv("SUPABASE_DB_URL")
S_SEC, S_WH = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY, T_EM = os.getenv("RESEND_API_KEY"), os.getenv("TEST_EMAIL")
T_SEC, P_URL = os.getenv("TRIGGER_SECRET"), os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# --- THE MASTER CRAWL LIST ---
# We provide the 'Building Address'. The engine will explore the sub-folders automatically.
COUNCILS = {
    "Leeds_Control": {
        "type": "direct",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Mega_Hub": {
        "type": "discovery",
        "root": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services",
        "home": "https://www.london.gov.uk/what-we-do/planning",
        "referer": "https://www.london.gov.uk/"
    },
    "Croydon_Direct": {
        "type": "discovery",
        "root": "https://maps.croydon.gov.uk/arcgis/rest/services/Planning",
        "home": "https://www.croydon.gov.uk/planning-and-regeneration",
        "referer": "https://www.croydon.gov.uk/"
    },
    "Woking_Surrey": {
        "type": "discovery",
        "root": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services",
        "home": "https://www.woking.gov.uk/planning",
        "referer": "https://www.woking.gov.uk/"
    }
}

# --- LOGIC: CLASSIFICATION ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "oak", "ash", "cedar", "conifer", "birch", "maple", "willow", "sycamore"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "extension", "loft conversion", "demolition"]

def get_d(r):
    v = r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("DATEAPVAL") or r.get("RECDAT") or 0
    try: return float(v)
    except: return 0

def classify(r):
    p = str(r.get("development_description") or r.get("PROPOSAL") or r.get("DESCRIPTION") or r.get("DESCRIPT") or "").lower()
    if not p: return False, 0
    matches = [k for k in TREE_WORDS if k in p]
    score = len(matches)
    if "tree" in p: score += 2
    if any(x in p for x in ["fell", "remove", "crown", "tpo", "conservation area"]): score += 5
    if any(w in p for w in SKIP_WORDS) and score < 8: return False, 0
    return (score > 2), score

# --- THE DISCOVERY ENGINE (V22) ---
def fetch_council(name, config):
    session = requests.Session()
    # High-Entropy Browser Fingerprint
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Referer": config["referer"],
        "Connection": "keep-alive"
    }
    q = {"where": "1=1", "outFields": "*", "resultRecordCount": 50, "orderByFields": "OBJECTID DESC", "f": "json"}

    # Step 1: The 'Handshake' - visit the home page to get session cookies
    if "home" in config:
        try:
            session.get(config["home"], headers=h, timeout=15, verify=False)
        except: pass

    # Step 2: Access Data
    if config["type"] == "direct":
        try:
            res = session.get(config["url"], params=q, headers=h, timeout=20, verify=False)
            if res.status_code == 200:
                return [f.get("attributes", {}) for f in res.json().get("features", [])], "Success"
        except: pass

    if config["type"] == "discovery":
        try:
            # Recursive Directory Crawl: Find subfolders then services
            root_res = session.get(f"{config['root']}?f=json", headers=h, timeout=15, verify=False)
            if "html" in root_res.text.lower():
                return [], "Firewall: Challenge Triggered"
            
            data = root_res.json()
            folders = [""] + data.get("folders", [])
            
            for folder in folders:
                folder_path = f"/{folder}" if folder else ""
                dir_res = session.get(f"{config['root']}{folder_path}?f=json", headers=h, timeout=10, verify=False)
                services = dir_res.json().get("services", [])
                
                # Search for keywords in service names
                for s in services:
                    sname = s.get("name", "").lower()
                    if any(k in sname for k in ["planning", "register", "development"]):
                        stype = s.get("type", "MapServer")
                        # Priority probe Layer 0 (standard) then Layer 5 (Surrey standard)
                        for layer_id in [0, 5, 12]:
                            discovery_url = f"{config['root']}/{s.get('name')}/{stype}/{layer_id}/query"
                            try:
                                res = session.get(discovery_url, params=q, headers=h, timeout=15, verify=False)
                                if res.status_code == 200 and "features" in res.text:
                                    return [f.get("attributes", {}) for f in res.json().get("features", [])], f"Cracked: {s.get('name')} (L{layer_id})"
                            except: continue
        except Exception as e:
            return [], f"Discovery Fail: {str(e)}"

    return [], "Offline/Service Hidden"

# --- DATABASE & ROUTES (Preserved Logic) ---
def is_already_sent(ref):
    if not SURL: return False
    try:
        conn = psycopg2.connect(SURL); cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS sent_leads (ref TEXT PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT NOW());")
        conn.commit()
        cur.execute("SELECT 1 FROM sent_leads WHERE ref = %s", (ref,))
        exists = cur.fetchone() is not None; conn.close()
        return exists
    except: return False

def mark_as_sent(ref):
    if not SURL: return
    try:
        conn = psycopg2.connect(SURL); cur = conn.cursor()
        cur.execute("INSERT INTO sent_leads (ref) VALUES (%s) ON CONFLICT DO NOTHING", (ref,))
        conn.commit(); conn.close()
    except: pass

@app.get("/", response_class=HTMLResponse)
def lander():
    return f"""
    <html><body style='font-family:sans-serif; text-align:center; padding-top:50px; background:#f4f4f9;'>
    <div style='display:inline-block; padding:40px; background:white; border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,0.05); border-top: 5px solid #2e7d32;'>
    <h1>Vector Data Labs V22.0</h1>
    <p>Leeds Baseline: <b>ACTIVE</b> | Discovery Engine: <b>ON</b></p>
    <hr style='border:0; border-top:1px solid #eee; margin:20px 0;'/>
    <a href='/test-regional' style='color:#1a73e8; text-decoration:none; font-weight:bold;'>Run Master Diagnostic</a>
    </div>
    </body></html>
    """

@app.get("/test-regional")
def test_all():
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_council(name, config)
        found = [r for r in recs if classify(r)[0]]
        results[name] = {"status": status, "scanned": len(recs), "tree_leads": len(found)}
    return results

@app.get("/trigger-scrape")
def scrape(secret: str = Query(...)):
    if secret != T_SEC: raise HTTPException(status_code=401)
    leads_sent = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    for c_name, config in COUNCILS.items():
        recs, _ = fetch_council(c_name, config)
        for r in recs:
            ref = str(r.get("REFERENCE") or r.get("PLANNO") or r.get("REFVAL") or r.get("OBJECTID"))
            is_valid, _ = classify(r)
            if is_valid and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
                addr = r.get('full_address') or r.get('ADDRESS') or r.get('LOCATION') or r.get('SITE_ADDRESS')
                prop = r.get('development_description') or r.get('PROPOSAL') or r.get('DESCRIPTION')
                try:
                    ai = client.chat.completions.create(
                        model="gpt-4o-mini", response_format={"type": "json_object"},
                        messages=[{"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)."},
                        {"role": "user", "content": f"Addr: {addr} Prop: {prop}"}]
                    )
                    ld = json.loads(ai.choices[0].message.content)
                except: continue
                surgeons = [{"id": 1, "email": T_EM}]
                if SURL:
                    try:
                        db = psycopg2.connect(SURL); c = db.cursor()
                        c.execute("SELECT id, email FROM tree_surgeons WHERE active IS TRUE")
                        surgeons = [{"id": row[0], "email": row[1]} for row in c.fetchall()] or surgeons
                        db.close()
                    except: pass
                for sgn in surgeons:
                    amt = 6000 if ld.get("high_value") else 3500
                    checkout = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Lead: {ld.get('site_address')}"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", success_url=f"{P_URL}/payment-success",
                        cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(sgn["id"]), "ref": ref, "site_address": ld.get("site_address")}
                    )
                    email_html = f"<h2>New Lead: {c_name}</h2><p>{ld.get('scope_summary')}</p><a href='{checkout.url}'>Purchase Lead</a>"
                    requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [sgn["email"]], "subject": f"Lead: {ld.get('site_address')}", "html": email_html}, headers={"Authorization": f"Bearer {R_KEY}"})
                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 10: break
    return {"status": "success", "leads_sent": leads_sent}
