import os, json, logging, requests, psycopg2, stripe, urllib3, time
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="Vector Data Labs - V32.0 Tiered Master", docs_url="/docs")
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

# --- THE MASTER DATA ARCHITECTURE (V32.0 Borough Side-Doors) ---
COUNCILS = {
    "Leeds_Baseline": {
        "type": "arcgis",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Official_API": {
        "type": "rest_api",
        # Using the stable root host to fix the DNS resolution error
        "url": "https://data.london.gov.uk/api/planning/v1/applications/",
        "params": {"page_size": 100}
    },
    "Southwark_Side_Door": {
        "type": "arcgis",
        # Hitting a direct high-volume borough instead of the hub
        "url": "https://geo.southwark.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
        "referer": "https://www.southwark.gov.uk/"
    },
    "Croydon_Side_Door": {
        "type": "arcgis",
        # Direct local borough building
        "url": "https://maps.croydon.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
        "referer": "https://www.croydon.gov.uk/"
    }
}

# --- TIERED SEARCH LOGIC (Human Logic Applied) ---

# TIER 1: The "Wide Net" (Construction, Development, Infrastructure)
WIDE_NET = [
    "planning", "development", "construction", "renovation", "extension", 
    "demolition", "landscape", "garden", "site", "works", "erection", 
    "alteration", "reconstruction", "infrastructure", "nature"
]

# TIER 2: The "Refined Search" (Tree Surgery Specifics)
TREE_SURGERY = [
    "tree", "tpo", "fell", "felling", "crown", "pruning", "stump", 
    "oak", "ash", "sycamore", "cedar", "birch", "willow", "pine", "reduction"
]

def smart_classify(record):
    """Refines search layer by layer to identify high-value leads."""
    # Find the data column (description)
    description = ""
    headers = ["proposal", "description", "development_description", "nature_of_work", "details", "PROPOSAL"]
    for h in headers:
        if record.get(h):
            description = str(record.get(h)).lower()
            break
    
    if not description:
        return False, 0

    # Step 1: Broad Check (Are we looking at construction/development?)
    if not any(word in description for word in WIDE_NET):
        return False, 0

    # Step 2: Refined Check (Is it specifically about trees?)
    matches = [word for word in TREE_SURGERY if word in description]
    score = len(matches)
    
    # Priority weighting
    if "tree" in description: score += 5
    if any(x in description for x in ["fell", "felling", "tpo"]): score += 10

    # Minimum threshold to avoid 'noise' (e.g. house with a single tree mention)
    return (score >= 12), score

# --- DATA ACQUISITION ENGINE ---
def fetch_council(name, config):
    session = requests.Session()
    # "High-Vis Vest" Header: Identifying as a legitimate professional tool
    h = {
        "User-Agent": "VectorDataLabs/32.0 (Open-Data Business Integration; contact: admin@vectordata.labs)",
        "Accept": "application/json",
        "Referer": config.get("referer", "https://www.google.com")
    }

    try:
        if config["type"] == "arcgis":
            q = {"where": "1=1", "outFields": "*", "resultRecordCount": 100, "orderByFields": "OBJECTID DESC", "f": "json"}
            res = session.get(config["url"], params=q, headers=h, timeout=30, verify=False)
        else:
            # REST API Path
            res = session.get(config["url"], params=config.get("params"), headers=h, timeout=30)

        if res.status_code != 200:
            return [], f"HTTP {res.status_code} Error"

        data = res.json()
        if config["type"] == "arcgis":
            records = [f.get("attributes", {}) for f in data.get("features", [])]
        else:
            records = data.get("results", [])
            
        return records, "Online"

    except Exception as e:
        return [], f"Connection Fault: {str(e)}"

# --- DATABASE & WEBHOOKS ---
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
    <html><body style='font-family:sans-serif; text-align:center; padding-top:50px; background:#fafafa;'>
    <div style='display:inline-block; padding:40px; background:white; border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,0.05); border-top: 5px solid #2e7d32;'>
    <h1>Vector Data Labs V32.0</h1>
    <p>Leeds Baseline: <b>ACTIVE</b> | London Strategy: <b>SIDE-DOORS</b></p>
    <p style='color:green;'>Tiered Search & Official Identity Enabled.</p>
    <hr style='border:0; border-top:1px solid #eee; margin:20px 0;'/>
    <a href='/test-regional' style='color:#2e7d32; text-decoration:none; font-weight:bold;'>Run Full System Diagnostic</a>
    </div>
    </body></html>
    """

@app.get("/test-regional")
def test_all():
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_council(name, config)
        found = [r for r in recs if smart_classify(r)[0]]
        results[name] = {"status": status, "records_found": len(recs), "tree_leads": len(found)}
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
            
            if is_valid and not is_already_sent(ref):
                addr = r.get('full_address') or r.get('ADDRESS') or r.get('LOCATION')
                prop = ""
                for h in ["proposal", "description", "development_description", "PROPOSAL"]:
                    if r.get(h): prop = r.get(h); break
                
                try:
                    ai = client.chat.completions.create(
                        model="gpt-4o-mini", 
                        response_format={"type": "json_object"},
                        messages=[
                            {"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)."},
                            {"role": "user", "content": f"Addr: {addr} Prop: {prop}"}
                        ]
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
                    email_html = f"<h2>New Lead: {c_name}</h2><p>{ld.get('scope_summary')}</p><a href='{checkout.url}'>Purchase Lead Details</a>"
                    requests.post(R_URL, json={"from": "Vector Data Labs <leads@resend.dev>", "to": [sgn["email"]], "subject": f"Lead: {ld.get('site_address')}", "html": email_html}, headers={"Authorization": f"Bearer {R_KEY}"})
                
                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 10: break
    return {"status": "success", "leads_sent": leads_sent}
