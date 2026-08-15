import os, json, logging, requests, psycopg2, stripe, urllib3, re
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="Vector Data Labs - V16.7 Master", docs_url="/docs")
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

# --- THE MASTER LIST (Organization Roots) ---
# We point to the top-level Organization ID. The engine discovers the active service.
COUNCILS = {
    "Leeds_Control": {
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Mega_Hub": {
        "org_root": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services",
        "referer": "https://www.london.gov.uk/"
    },
    "Richmond_Wandsworth": {
        "org_root": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services",
        "referer": "https://www.wandsworth.gov.uk/"
    },
    "Woking_Surrey": {
        "org_root": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services",
        "referer": "https://www.woking.gov.uk/"
    },
    "Croydon_Direct": {
        "org_root": "https://maps.croydon.gov.uk/arcgis/rest/services/Planning",
        "referer": "https://maps.croydon.gov.uk/planning/index.html"
    }
}

# --- LOGIC: CLASSIFICATION ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "oak", "ash ", "cedar", "conifer", "birch", "maple", "willow", "sycamore"]
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
    if any(x in p for x in ["fell", "remove", "crown", "tpo"]): score += 5
    if any(w in p for w in SKIP_WORDS) and score < 8: return False, 0
    return (score > 2), score

# --- FETCHING LOGIC (The Master Discovery V16.7) ---
def fetch_council(name, config):
    session = requests.Session()
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": config["referer"],
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
        "Connection": "keep-alive"
    }
    q = {"where": "1=1", "outFields": "*", "resultRecordCount": 50, "orderByFields": "OBJECTID DESC", "f": "json"}

    # 1. Standard Query (If direct URL exists like Leeds)
    if "url" in config:
        try:
            res = session.get(config["url"], params=q, headers=h, timeout=20, verify=False)
            if res.status_code == 200 and "features" in res.text:
                return [f.get("attributes", {}) for f in res.json().get("features", [])], "Success"
        except: pass

    # 2. Master Discovery Logic
    if "org_root" in config:
        try:
            # Step A: List all services in the building
            meta_res = session.get(f"{config['org_root']}?f=json", headers=h, timeout=15, verify=False)
            if "application/json" not in meta_res.headers.get("Content-Type", ""):
                return [], "Firewall: Blocked (HTML Response)"
            
            services = meta_res.json().get("services", [])
            # Step B: Filter for Planning services
            planning_services = [s for s in services if any(k in s['name'].lower() for k in ["planning", "development", "register"])]
            
            for s in planning_services:
                s_name = s['name']
                s_type = s['type'] # MapServer or FeatureServer
                
                # Step C: Layer Spray (Try indices that commonly hold data)
                for l_id in [0, 5, 12, 1]:
                    probe_url = f"{config['org_root']}/{s_name}/{s_type}/{l_id}/query"
                    try:
                        probe = session.get(probe_url, params=q, headers=h, timeout=10, verify=False)
                        if probe.status_code == 200 and "features" in probe.text:
                            data = probe.json()
                            features = data.get("features", [])
                            if len(features) > 0:
                                return [f.get("attributes", {}) for f in features], f"Cracked: {s_name} (L{l_id})"
                    except: continue
        except Exception as e:
            return [], f"Discovery Error: {str(e)}"

    return [], "Status: Service Hidden/Private"

# --- DATABASE ---
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

# --- ROUTES ---
@app.get("/", response_class=HTMLResponse)
def lander():
    return f"""
    <html><body style='font-family:sans-serif;text-align:center;padding-top:50px; background:#f4f4f9;'>
    <div style='display:inline-block; padding:40px; background:white; border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,0.05); border-top: 5px solid #2e7d32;'>
    <h1>Vector Data Labs V16.7</h1>
    <p>Leeds: <b>ACTIVE</b> | Discovery Engine: <b>ROOT SCANNING</b></p>
    <hr style='border:0; border-top:1px solid #eee; margin:20px 0;'/>
    <a href='/test-regional' style='color:#1a73e8; text-decoration:none; font-weight:bold;'>Run Master Health Check</a>
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
            is_tree, _ = classify(r)
            
            if is_tree and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
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

                surgeons = []
                if SURL:
                    try:
                        db = psycopg2.connect(SURL); c = db.cursor()
                        c.execute("SELECT id, email FROM tree_surgeons WHERE active IS TRUE")
                        for row in c.fetchall(): surgeons.append({"id": row[0], "email": row[1]})
                        db.close()
                    except: pass
                if not surgeons: surgeons.append({"id": 1, "email": T_EM})

                for sgn in surgeons:
                    amt = 6000 if ld.get("high_value") else 3500
                    checkout = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Lead: {ld.get('site_address')}"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", success_url=f"{P_URL}/payment-success",
                        cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(sgn["id"]), "ref": ref, "site_address": ld.get("site_address")}
                    )
                    email_html = f"""
                    <div style='font-family:sans-serif; border-left: 8px solid #1a73e8; padding:20px; background:#f9f9f9;'>
                    <h2 style='color:#1a73e8;'>New Tree Lead: {c_name}</h2>
                    <p><strong>Work:</strong> {ld.get('scope_summary')}</p>
                    <p><strong>Location:</strong> {ld.get('site_address')}</p>
                    <br/>
                    <a href='{checkout.url}' style='background:#1a73e8; color:white; padding:15px 30px; text-decoration:none; border-radius:5px; font-weight:bold; display:inline-block;'>Buy Lead Details (£{amt/100})</a>
                    </div>
                    """
                    requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [sgn["email"]], "subject": f"Lead: {ld.get('site_address')}", "html": email_html}, headers={"Authorization": f"Bearer {R_KEY}"})
                
                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 10: break
        if leads_sent >= 10: break
    return {"status": "success", "leads_sent": leads_sent}

@app.get("/payment-success")
def success(): return HTMLResponse("<h1>Success!</h1>")

@app.get("/payment-cancelled")
def cancel(): return HTMLResponse("<h1>Cancelled</h1>")
