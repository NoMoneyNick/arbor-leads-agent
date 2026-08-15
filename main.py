import os, json, logging, requests, psycopg2, stripe, urllib3, time
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Professional Identity & Stability Setup
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = FastAPI(title="Vector Data Labs - V28.0 Master Discovery", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# --- ENVIRONMENT ---
OKEY, SURL = os.getenv("OPENAI_API_KEY"), os.getenv("SUPABASE_DB_URL")
S_SEC, S_WH = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY, T_EM = os.getenv("RESEND_API_KEY"), os.getenv("TEST_EMAIL")
T_SEC, P_URL = os.getenv("TRIGGER_SECRET"), os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# --- THE MASTER "SIDE DOORS" (Direct Borough Roots) ---
COUNCILS = {
    "Leeds_Control": {
        "type": "static",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Mega_Hub": {
        "type": "discovery",
        "root": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services",
        "referer": "https://www.london.gov.uk/"
    },
    "Croydon_Direct": {
        "type": "discovery",
        "root": "https://maps.croydon.gov.uk/arcgis/rest/services/Planning",
        "referer": "https://www.croydon.gov.uk/"
    },
    "Hillingdon_Direct": {
        "type": "discovery",
        "root": "https://maps.hillingdon.gov.uk/arcgis/rest/services/PublicServices",
        "referer": "https://www.hillingdon.gov.uk/"
    }
}

# --- LOGIC: THE WIDE NET ---
# Step 1: Identifying the right column labels
HEADER_KEYWORDS = ["proposal", "description", "nature", "work", "natureofwork", "details", "devdesc"]
# Step 2: Refining to tree-specific surgery leads
TREE_SPECIFIC = ["tree", "tpo", "fell", "crown", "pruning", "oak", "ash", "sycamore", "cedar", "birch"]

def smart_classify(record):
    """Human Logic: Look for the cabinet first, then the files."""
    # A. Find the 'Description' column by looking at all headers
    description = ""
    for key, value in record.items():
        if any(hk in key.lower() for hk in HEADER_KEYWORDS):
            description = str(value).lower()
            break
    
    if not description: return False, 0

    # B. Score for Tree Surgery
    score = sum(2 for word in TREE_SPECIFIC if word in description)
    if "tree" in description: score += 5
    
    # Filter out small mentions
    return (score > 4), score

# --- FETCHING ENGINE (The Identity Handshake V28) ---
def fetch_council(name, config):
    session = requests.Session()
    # "High-Vis Vest" Headers: Be transparent and professional to pass firewalls
    h = {
        "User-Agent": "VectorDataLabs/1.0 (Integration for Local Business Growth; contact: admin@vectordata.labs)",
        "Accept": "application/json, text/plain, */*",
        "Referer": config["referer"],
        "Connection": "keep-alive"
    }
    q = {"where": "1=1", "outFields": "*", "resultRecordCount": 50, "orderByFields": "OBJECTID DESC", "f": "json"}

    if config["type"] == "static":
        try:
            res = session.get(config["url"], params=q, headers=h, timeout=20, verify=False)
            return [f.get("attributes", {}) for f in res.json().get("features", [])], "Success"
        except: pass

    if config["type"] == "discovery":
        try:
            # Step A: Ask the building directory for a list of rooms (services)
            catalog = session.get(f"{config['root']}?f=json", headers=h, timeout=15, verify=False).json()
            # Filter for services that sound like planning
            services = [s for s in catalog.get("services", []) if any(k in s['name'].lower() for k in ["planning", "register", "application", "live"])]
            
            for s in services:
                # Step B: Try the most common desk IDs (Layers 0, 5, 12)
                for layer_id in [0, 5, 12, 1]:
                    target_url = f"{config['root']}/{s['name']}/{s['type']}/{layer_id}/query"
                    try:
                        res = session.get(target_url, params=q, headers=h, timeout=12, verify=False)
                        if res.status_code == 200 and "features" in res.text:
                            data = res.json().get("features", [])
                            if len(data) > 0:
                                # We found a room with data!
                                return [f.get("attributes", {}) for f in data], f"Side-Door Found: {s['name']} (L{layer_id})"
                    except: continue
        except Exception as e:
            return [], f"Discovery Failed: {str(e)}"

    return [], "Offline or Locked"

# --- DATABASE & ROUTES ---
def is_already_sent(ref):
    if not SURL: return False
    try:
        conn = psycopg2.connect(SURL); cur = conn.cursor()
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

@app.get("/test-regional")
def test_all():
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_council(name, config)
        found = [r for r in recs if smart_classify(r)[0]]
        results[name] = {"status": status, "records_scanned": len(recs), "tree_leads": len(found)}
    return results

@app.get("/trigger-scrape")
def scrape(secret: str = Query(...)):
    if secret != T_SEC: raise HTTPException(status_code=401)
    leads_sent = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    for c_name, config in COUNCILS.items():
        recs, _ = fetch_council(c_name, config)
        for r in recs:
            ref = str(r.get("external_system_reference") or r.get("REFERENCE") or r.get("PLANNO") or r.get("OBJECTID"))
            is_valid, _ = smart_classify(r)
            if is_valid and not is_already_sent(ref):
                # Process lead via AI
                addr = r.get('full_address') or r.get('ADDRESS') or r.get('LOCATION') or r.get('SITE_ADDRESS')
                # Dynamically find the description field for the AI
                prop = ""
                for k, v in r.items():
                    if any(hk in k.lower() for hk in HEADER_KEYWORDS): prop = v; break
                
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

@app.get("/", response_class=HTMLResponse)
def home():
    return "<h1>Vector Data Labs V28.0</h1><p>Active Discovery Engine Online. See /docs.</p>"
