import os, json, logging, requests, psycopg2, stripe, urllib3, time
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Professional Stability Setup
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = FastAPI(title="Vector Data Labs - V42.0 Discovery Master", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# --- ENVIRONMENT ---
OKEY = os.getenv("OPENAI_API_KEY")
SURL = os.getenv("SUPABASE_DB_URL")
S_SEC = os.getenv("STRIPE_SECRET_KEY")
R_KEY = os.getenv("RESEND_API_KEY")
T_EM = os.getenv("TEST_EMAIL") # Your notification email
T_SEC = os.getenv("TRIGGER_SECRET") # Your scrape password
P_URL = os.getenv("PUBLIC_APP_URL")
GLA_KEY = os.getenv("GLA_API_KEY") 

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC

# --- DATA ARCHITECTURE (V42.0 Verified) ---
COUNCILS = {
    "Leeds_City_Control": {
        "type": "arcgis",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "Manchester_City_Control": {
        "type": "arcgis",
        "url": "https://pa.manchester.gov.uk/arcgis/rest/services/Public/Planning_Applications/MapServer/0/query",
        "referer": "https://www.manchester.gov.uk/planning"
    },
    "London_Official_Hub": {
        "type": "ckan",
        "url": "https://data.london.gov.uk/api/3/action/datastore_search",
        "resource_id": "847f2b1a-3852-475a-bcaf-192a29792664",
        "params": {"limit": 100}
    }
}

# --- CLASSIFICATION LOGIC ---
CABINET_HEADERS = ["proposal", "description", "development_description", "nature", "details", "PROPOSAL"]
TREE_GOLD = ["tree", "tpo", "fell", "felling", "arboriculture", "crown", "pruning", "stump", "oak", "ash", "willow", "cedar"]
PLANNING_CONTEXT = ["planning", "development", "construction", "extension", "works", "site"]

def smart_classify(record):
    """3-Tier refinement to identify tree surgery leads."""
    description = ""
    for key, value in record.items():
        if any(h in key.lower() for h in CABINET_HEADERS):
            description = str(value).lower()
            break
    
    if not description: return False, 0

    tree_matches = [word for word in TREE_GOLD if word in description]
    if tree_matches:
        score = len(tree_matches) * 5
        if any(x in description for x in ["fell", "felling", "tpo"]): score += 15
        return (score >= 10), score

    return False, 0

def get_d(r):
    """Standardizes date formats across different Council APIs."""
    v = r.get("received_date") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or 0
    if isinstance(v, str):
        try: return datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp() * 1000
        except: return 0
    return float(v)

# --- AUTHORIZED DATA ENGINE ---
def fetch_council(name, config):
    session = requests.Session()
    headers = {
        "User-Agent": "VectorDataLabs/42.0 (Professional Data Integration)",
        "Accept": "application/json"
    }
    try:
        if config["type"] == "ckan":
            if GLA_KEY: headers["Authorization"] = GLA_KEY
            params = {**config.get("params", {}), "resource_id": config["resource_id"]}
            res = session.get(config["url"], params=params, headers=headers, timeout=30)
            if res.status_code == 200:
                return res.json().get("result", {}).get("records", []), "Online"
            return [], f"API Error: {res.status_code}"

        elif config["type"] == "arcgis":
            headers["Referer"] = config["referer"]
            # Requesting last 100 entries sorted by ID
            query_params = {
                "where": "1=1", 
                "outFields": "*", 
                "resultRecordCount": 100, 
                "orderByFields": "OBJECTID DESC", 
                "f": "json"
            }
            res = session.get(config["url"], params=query_params, headers=headers, timeout=30, verify=False)
            if res.status_code == 200:
                return [f.get("attributes", {}) for f in res.json().get("features", [])], "Online"
            return [], f"HTTP {res.status_code}"

    except Exception as e:
        return [], f"Fault: {str(e)}"
    return [], "Offline"

# --- PERSISTENCE LAYER ---
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

# --- APP INTERFACE ---
@app.get("/")
def lander():
    return f"""
    <html><body style='font-family:sans-serif; text-align:center; padding-top:50px; background:#f4f4f9;'>
    <div style='display:inline-block; padding:50px; background:white; border-radius:15px; box-shadow:0 10px 30px rgba(0,0,0,0.1); border-top: 6px solid #1b5e20; max-width:600px;'>
        <h1 style='color:#1b5e20;'>Vector Data Labs</h1>
        <p>Lead-Gen Integration Hub V42.0</p>
        <div style='background:#f1f8e9; padding:15px; border-radius:10px; margin:20px 0; text-align:left; font-size:14px;'>
            <b>Active Baselines:</b> Leeds, Manchester<br/>
            <b>Awaiting Handshake:</b> London GLA, Croydon
        </div>
        <a href='/test-regional' style='display:inline-block; padding:12px 25px; background:#1b5e20; color:white; text-decoration:none; border-radius:5px; font-weight:bold;'>Verify Regional Feeds</a>
    </div>
    </body></html>
    """

@app.get("/test-regional")
def test_all():
    """Diagnostic route to check connection status of all targets."""
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_council(name, config)
        found = [r for r in recs if smart_classify(r)[0]]
        results[name] = {"status": status, "scanned": len(recs), "tree_leads": len(found)}
    return results

@app.get("/trigger-scrape")
def scrape(secret: str = Query(...)):
    """Primary automation route to process leads and generate Stripe checkouts."""
    if secret != T_SEC: raise HTTPException(status_code=401)
    leads_sent = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    
    for c_name, config in COUNCILS.items():
        recs, _ = fetch_council(c_name, config)
        for r in recs:
            # Detect the best reference ID for the lead
            ref = str(r.get("REFERENCE") or r.get("OBJECTID") or r.get("PLAN_APP_REF") or r.get("_id"))
            is_valid, _ = smart_classify(r)
            
            if is_valid and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
                addr = r.get('full_address') or r.get('ADDRESS') or r.get('LOCATION') or r.get('SITE_ADDRESS') or "Address not provided"
                prop = next((v for k, v in r.items() if any(h in k.lower() for h in CABINET_HEADERS)), "No description")
                
                try:
                    # AI Scoring and Summarization
                    ai_res = client.chat.completions.create(
                        model="gpt-4o-mini", 
                        response_format={"type": "json_object"},
                        messages=[{"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool). Focus on tree removal vs maintenance."},
                                  {"role": "user", "content": f"Address: {addr} Proposal: {prop}"}]
                    )
                    ld = json.loads(ai_res.choices[0].message.content)
                except: continue

                # Targeting Surgeons (Fallback to test email)
                surgeons = [{"id": 1, "email": T_EM}]
                # If Supabase is connected, fetch active surgeons here

                for sgn in surgeons:
                    price = 6000 if ld.get("high_value") else 3500
                    checkout = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Lead: {ld.get('site_address')}"}, "unit_amount": price}, "quantity": 1}],
                        mode="payment", 
                        success_url=f"{P_URL}/payment-success", 
                        cancel_url=f"{P_URL}/payment-cancelled"
                    )

                    # Dispatch via Resend
                    email_html = f"<h2>New Lead: {c_name}</h2><p>{ld.get('scope_summary')}</p><a href='{checkout.url}'>Purchase Lead</a>"
                    requests.post(R_URL, json={
                        "from": "Vector Data Labs <onboarding@resend.dev>",
                        "to": [sgn["email"]],
                        "subject": f"New Lead: {ld.get('site_address')}",
                        "html": email_html
                    }, headers={"Authorization": f"Bearer {R_KEY}"})

                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 10: break

    return {"status": "success", "leads_sent": leads_sent}

@app.get("/payment-success")
def success(): return HTMLResponse("<h1>Payment Successful</h1>")

@app.get("/payment-cancelled")
def cancel(): return HTMLResponse("<h1>Payment Cancelled</h1>")
