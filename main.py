import os, json, logging, requests, psycopg2, stripe, urllib3, time
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Professional Stability Setup
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = FastAPI(title="Vector Data Labs - V34.0 Official Integration", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# --- ENVIRONMENT ---
OKEY, SURL = os.getenv("OPENAI_API_KEY"), os.getenv("SUPABASE_DB_URL")
S_SEC, S_WH = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY, T_EM = os.getenv("RESEND_API_KEY"), os.getenv("TEST_EMAIL")
T_SEC, P_URL = os.getenv("TRIGGER_SECRET"), os.getenv("PUBLIC_APP_URL")
GLA_KEY = os.getenv("GLA_API_KEY") # Your Official Key to the Building

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# --- THE MASTER ARCHITECTURE (V34.0 Official Hubs) ---
COUNCILS = {
    "Leeds_City_Baseline": {
        "type": "arcgis",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Boroughs_Unified": {
        "type": "datapress",
        # The Official Data Artery for all 32 London Boroughs
        "url": "https://data.london.gov.uk/api/planning/v1/applications/",
        "params": {"page_size": 100}
    },
    "Surrey_Shared_Hub": {
        "type": "arcgis_discovery",
        # Physical cluster for Surrey councils - looking for the 'Live' application room
        "root": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services",
        "referer": "https://www.woking.gov.uk/"
    }
}

# --- HUMAN LOGIC: TIERED REFINEMENT ---
CABINET_HEADERS = ["proposal", "description", "development_description", "nature", "details"]
CONSTRUCTION_WORDS = ["planning", "development", "construction", "renovation", "extension", "works", "site", "landscape"]
TREE_WORDS = ["tree", "tpo", "fell", "felling", "crown", "pruning", "stump", "oak", "ash", "sycamore", "cedar", "birch", "willow"]

def smart_classify(record):
    """Tiered search: Construction -> Tree Specifics."""
    description = ""
    for key, value in record.items():
        if any(h in key.lower() for h in CABINET_HEADERS):
            description = str(value).lower()
            break
    if not description: return False, 0
    if not any(word in description for word in CONSTRUCTION_WORDS): return False, 0
    score = sum(4 for word in TREE_WORDS if word in description)
    if "tree" in description: score += 5
    if any(x in description for x in ["fell", "tpo", "conservation area"]): score += 10
    return (score >= 12), score

def get_d(r):
    v = r.get("received_date") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or 0
    if isinstance(v, str):
        try: return datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp() * 1000
        except: return 0
    return float(v)

# --- THE OFFICIAL DATA ENGINE ---
def fetch_council(name, config):
    session = requests.Session()
    h = {
        "User-Agent": "VectorDataLabs/34.0 (Official Data Integration; contact: admin@vectordata.labs)",
        "Accept": "application/json",
        "Referer": config.get("referer", "https://www.google.com")
    }
    if GLA_KEY: h["X-Api-Key"] = GLA_KEY # Sending your Passport

    try:
        if config["type"] == "datapress":
            res = session.get(config["url"], params=config["params"], headers=h, timeout=30)
            if res.status_code == 200:
                return res.json().get("results", []), "Online (Authenticated)"
            return [], f"API Response: {res.status_code}"

        elif config["type"] == "arcgis":
            q = {"where": "1=1", "outFields": "*", "resultRecordCount": 100, "orderByFields": "OBJECTID DESC", "f": "json"}
            res = session.get(config["url"], params=q, headers=h, timeout=30, verify=False)
            return [f.get("attributes", {}) for f in res.json().get("features", [])], "Online"

        elif config["type"] == "arcgis_discovery":
            # Surrey Fix: Discovery logic to find the 'Planning' room instead of guessing
            dir_res = session.get(f"{config['root']}?f=json", headers=h, timeout=15, verify=False).json()
            for s in dir_res.get("services", []):
                if any(k in s['name'] for k in ["Planning", "Applications", "Live"]):
                    query_url = f"{config['root']}/{s['name']}/{s['type']}/0/query"
                    q = {"where": "1=1", "outFields": "*", "resultRecordCount": 50, "f": "json"}
                    res = session.get(query_url, params=q, headers=h, timeout=15, verify=False)
                    if res.status_code == 200:
                        return [f.get("attributes", {}) for f in res.json().get("features", [])], f"Found: {s['name']}"
    except Exception as e:
        return [], f"Fault: {str(e)}"
    return [], "Offline/Check Key"

# --- DATABASE & WEBHOOKS (Preserved) ---
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
@app.get("/")
def lander():
    return f"""
    <html><body style='font-family:sans-serif; text-align:center; padding-top:50px; background:#f4f4f9;'>
    <div style='display:inline-block; padding:50px; background:white; border-radius:15px; box-shadow:0 10px 30px rgba(0,0,0,0.1); border-top: 6px solid #1b5e20; max-width:600px;'>
    <h1 style='color:#1b5e20;'>Vector Data Labs</h1>
    <p>Official Developer Integration Hub V34.0</p>
    <div style='background:#f1f8e9; padding:15px; border-radius:10px; margin:20px 0; text-align:left; font-size:14px;'>
    <b>Status:</b> Leeds Active | London Hub Authenticated.<br/>
    <b>Logic:</b> Tiered Keyword Discovery (Construction -> Tree).
    </div>
    <a href='/test-regional' style='display:inline-block; padding:12px 25px; background:#1b5e20; color:white; text-decoration:none; border-radius:5px; font-weight:bold;'>Check Live Leads Feed</a>
    </div>
    </body></html>
    """

@app.get("/test-regional")
def test_all():
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_council(name, config)
        found = [r for r in recs if smart_classify(r)[0]]
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
            ref = str(r.get("external_system_reference") or r.get("REFERENCE") or r.get("OBJECTID"))
            is_valid, _ = smart_classify(r)
            if is_valid and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
                addr = r.get('full_address') or r.get('ADDRESS') or r.get('LOCATION') or r.get('SITE_ADDRESS')
                prop = ""
                for k, v in r.items():
                    if any(h in k.lower() for h in CABINET_HEADERS): prop = v; break
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
                        mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(sgn["id"]), "ref": ref, "site_address": ld.get("site_address")}
                    )
                    email_html = f"<h2>New Lead: {c_name}</h2><p>{ld.get('scope_summary')}</p><a href='{checkout.url}'>Purchase Lead</a>"
                    requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [sgn["email"]], "subject": f"Lead: {ld.get('site_address')}", "html": email_html}, headers={"Authorization": f"Bearer {R_KEY}"})
                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 10: break
    return {"status": "success", "leads_sent": leads_sent}

@app.get("/payment-success", include_in_schema=False)
def success(): return HTMLResponse("<h1>Success!</h1>")

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return HTMLResponse("<h1>Cancelled</h1>")
