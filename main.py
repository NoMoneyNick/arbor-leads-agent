import os, json, logging, requests, psycopg2, stripe, urllib3
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Disable SSL warnings for internal council certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="Vector Data Labs - V21.0 Master", docs_url="/docs")
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

# --- THE MASTER ENDPOINTS (V21.0 High-Ground) ---
# We use 'Root' names and a retry list to handle council renames.
COUNCILS = {
    "Leeds_Control": {
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Mega_Hub": {
        "cluster": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services",
        "services": ["Planning_London_Datahub", "Planning_Applications", "GLA_Planning"],
        "referer": "https://www.london.gov.uk/"
    },
    "Woking_Surrey": {
        "cluster": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services",
        "services": ["Planning_Applications_Woking", "Woking_Planning", "Planning_Applications_Live"],
        "referer": "https://www.woking.gov.uk/"
    },
    "Croydon_Direct": {
        "url": "https://maps.croydon.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
        "referer": "https://maps.croydon.gov.uk/planning/index.html"
    }
}

# --- LOGIC: CLASSIFICATION ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "oak", "ash", "cedar", "conifer", "birch", "maple", "willow", "sycamore", "pine", "cypress"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "extension", "loft conversion", "demolition", "shopfront"]

def get_d(r):
    v = r.get("received_date") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("DATEAPVAL") or r.get("RECDAT") or 0
    if isinstance(v, str):
        try: return datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp() * 1000
        except: return 0
    return float(v)

def classify(r):
    p = str(r.get("development_description") or r.get("description") or r.get("PROPOSAL") or r.get("DESCRIPTION") or "").lower()
    if not p: return False, 0
    matches = [k for k in TREE_WORDS if k in p]
    score = len(matches)
    if "tree" in p: score += 2
    if any(x in p for x in ["fell", "remove", "crown", "tpo", "conservation area"]): score += 5
    if any(w in p for w in SKIP_WORDS) and score < 8: return False, 0
    return (score > 2), score

# --- FETCHING ENGINE (The Redundant Hub V21) ---
def fetch_council(name, config):
    session = requests.Session()
    h = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": config["referer"],
        "Connection": "keep-alive"
    }
    q = {"where": "1=1", "outFields": "*", "resultRecordCount": 100, "orderByFields": "OBJECTID DESC", "f": "json"}

    # Track 1: Static URL
    if "url" in config:
        try:
            res = session.get(config["url"], params=q, headers=h, timeout=20, verify=False)
            if res.status_code == 200 and "features" in res.text:
                return [f.get("attributes", {}) for f in res.json().get("features", [])], "Online"
        except: pass

    # Track 2: Cluster Discovery (Failover Logic)
    if "cluster" in config:
        for service_name in config["services"]:
            for s_type in ["FeatureServer", "MapServer"]:
                # We try Layer 0 (Standard) and Layer 5 (Woking/Surrey Standard)
                for layer_id in [0, 5]:
                    target_url = f"{config['cluster']}/{service_name}/{s_type}/{layer_id}/query"
                    try:
                        res = session.get(target_url, params=q, headers=h, timeout=15, verify=False)
                        if res.status_code == 200 and "features" in res.text:
                            features = res.json().get("features", [])
                            if len(features) > 0:
                                return [f.get("attributes", {}) for f in features], f"Online ({service_name} L{layer_id})"
                    except: continue

    return [], "Offline/Rotated"

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
    <div style='display:inline-block; padding:40px; background:white; border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,0.05); border-top: 5px solid #1a73e8;'>
    <h1>Vector Data Labs V21.0</h1>
    <p>Leeds: <b>ACTIVE</b> | Discovery Hub: <b>REDUNDANCY ENABLED</b></p>
    <hr style='border:0; border-top:1px solid #eee; margin:20px 0;'/>
    <a href='/test-regional' style='color:#1a73e8; text-decoration:none; font-weight:bold;'>Run Full System Health Check</a>
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
            # Multi-Source Reference generator
            ref = str(r.get("external_system_reference") or r.get("REFERENCE") or r.get("PLANNO") or r.get("OBJECTID"))
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

                surgeons = [{"id": 1, "email": T_EM}]
                if SURL:
                    try:
                        db = psycopg2.connect(SURL); c = db.cursor()
                        c.execute("SELECT id, email FROM tree_surgeons WHERE active IS TRUE")
                        surgeons = [{"id": row[0], "email": row[1]} for row in c.fetchall()] or surgeons
                        db.close()
                    except: pass

                for sgn in surgeons:
                    # London leads are high value; £35 base / £60 high-value
                    amt = 6000 if ld.get("high_value") else 3500
                    checkout = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Lead: {ld.get('site_address')}"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", success_url=f"{P_URL}/payment-success",
                        cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(sgn["id"]), "ref": ref, "site_address": ld.get("site_address")}
                    )
                    email_html = f"<h2>New Tree Lead: {c_name}</h2><p>{ld.get('scope_summary')}</p><a href='{checkout.url}'>Purchase Lead</a>"
                    requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [sgn["email"]], "subject": f"Lead: {ld.get('site_address')}", "html": email_html}, headers={"Authorization": f"Bearer {R_KEY}"})
                
                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 10: break
        if leads_sent >= 10: break
    return {"status": "success", "leads_sent": leads_sent}

@app.get("/payment-success", include_in_schema=False)
def success(): return HTMLResponse("<h1>Success!</h1>")

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return HTMLResponse("<h1>Cancelled</h1>")
