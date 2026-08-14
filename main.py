import os, json, logging, requests, psycopg2, stripe, urllib3
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Disable SSL warnings for councils with internal certificates (common in London)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="Vector Data Labs - V9.3 London Tactical", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# --- ENVIRONMENT (DNA PRESERVED) ---
OKEY, SURL = os.getenv("OPENAI_API_KEY"), os.getenv("SUPABASE_DB_URL")
S_SEC, S_WH = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY, T_EM = os.getenv("RESEND_API_KEY"), os.getenv("TEST_EMAIL")
T_SEC, P_URL = os.getenv("TRIGGER_SECRET"), os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# --- THE LONDON TACTICAL LIST (V9.3 Verified Direct Links) ---
COUNCILS = {
    "Leeds_Control": {
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Mega_Hub": {
        "url": "https://gis2.london.gov.uk/server/rest/services/apps/planning_data_map_02/MapServer/0/query",
        "referer": "https://www.london.gov.uk/"
    },
    "Southwark_London": {
        "url": "https://geo.southwark.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
        "referer": "https://www.southwark.gov.uk/"
    },
    "Croydon_London": {
        "url": "https://maps.croydon.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
        "referer": "https://www.croydon.gov.uk/"
    },
    "Wandsworth_Richmond": {
        "url": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications/FeatureServer/0/query",
        "referer": "https://www.wandsworth.gov.uk/"
    }
}

# --- WEB PAGES ---
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def lander():
    return f"""
    <html>
        <body style='font-family:sans-serif; text-align:center; padding-top:50px; background:#fafafa;'>
            <div style='display:inline-block; padding:40px; background:white; border-radius:10px; border-top:5px solid #2e7d32; box-shadow:0 4px 6px rgba(0,0,0,0.1);'>
                <h1>Vector Data Labs V9.3</h1>
                <p>System Online. Leeds Active. London Discovery in progress.</p>
                <hr style='border:0; border-top:1px solid #eee; margin:20px 0;'/>
                <a href='/test-regional' style='color:#2e7d32; text-decoration:none; font-weight:bold;'>Run Regional Health Check</a>
            </div>
        </body>
    </html>
    """

# --- LOGIC: CLASSIFICATION ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "oak", "ash ", "cedar", "conifer", "birch", "maple", "willow", "sycamore"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "extension", "loft conversion", "basement", "demolition"]

def get_d(r):
    v = r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("DATEAPVAL") or r.get("RECDAT") or 0
    try: return float(v)
    except: return 0

def classify(r):
    # Searches all possible description keys used across different London schemas
    p = str(
        r.get("development_description") or 
        r.get("PROPOSAL") or 
        r.get("DESCRIPTION") or 
        r.get("DESCRIPT") or 
        r.get("DETDESC") or ""
    ).lower()
    
    if not p: return False, 0
    
    matches = [k for k in TREE_WORDS if k in p]
    score = len(matches)
    
    if "tree" in p: score += 2
    if any(x in p for x in ["fell", "remove", "crown", "tpo", "conservation area"]): score += 5
    
    # Strict London filtering: Avoid extensions and new builds unless they are massive tree jobs
    if any(w in p for w in SKIP_WORDS) and score < 8: return False, 0
    
    return (score > 2), score

# --- LOGIC: FETCHING ---
def fetch_council(name, config):
    url = config["url"]
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Referer": config["referer"],
        "Accept": "application/json"
    }
    
    # Using a 100 record count for a wider net
    q = {
        "where": "1=1", 
        "outFields": "*", 
        "resultRecordCount": 100, 
        "orderByFields": "OBJECTID DESC", 
        "f": "json"
    }
    
    try:
        # verify=False is critical for London borough servers (Southwark/Croydon)
        res = requests.get(url, params=q, headers=h, timeout=20, verify=False)
        
        if res.status_code != 200:
            return [], f"HTTP {res.status_code} Error"
            
        data = res.json()
        if "error" in data:
            return [], f"ArcGIS Error: {data['error'].get('message')}"
        
        features = data.get("features", [])
        return [f.get("attributes", {}) for f in features], "Success"
        
    except Exception as e:
        return [], f"Connection Fail: {str(e)}"

# --- DATABASE ---
def is_already_sent(ref):
    if not SURL: return False
    try:
        conn = psycopg2.connect(SURL); cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS sent_leads (ref TEXT PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT NOW());")
        conn.commit(); cur.execute("SELECT 1 FROM sent_leads WHERE ref = %s", (ref,))
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

@app.get("/test-regional")
def test_all():
    """Scoreboard for London Expansion."""
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_council(name, config)
        found = [r for r in recs if classify(r)[0]]
        results[name] = {
            "status": status, 
            "scanned": len(recs), 
            "tree_leads": len(found)
        }
    return results

@app.get("/trigger-scrape")
def scrape(secret: str = Query(...)):
    if secret != T_SEC: raise HTTPException(status_code=401)
    
    leads_sent = 0
    for c_name, config in COUNCILS.items():
        recs, _ = fetch_council(c_name, config)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        
        for r in recs:
            ref = r.get("REFERENCE") or r.get("PLANNO") or r.get("REFVAL") or r.get("external_system_reference") or str(r.get("OBJECTID"))
            is_tree, _ = classify(r)
            
            if is_tree and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
                addr = r.get('full_address') or r.get('ADDRESS') or r.get('LOCATION') or r.get('SITE_ADDRESS')
                prop = r.get('development_description') or r.get('PROPOSAL') or r.get('DESCRIPTION')
                
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

                # Get Surgeons from Supabase
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
                    # London leads are higher value: £35 / £60
                    amt = 6000 if ld.get("high_value") else 3500
                    checkout = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"London Tree Lead: {ld.get('site_address')}"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(sgn["id"]), "ref": ref, "site_address": ld.get("site_address")}
                    )
                    
                    email_html = f"""
                    <div style='font-family:sans-serif; border-left: 10px solid #2e7d32; padding:20px; background:#f9f9f9;'>
                        <h2 style='color:#2e7d32;'>Exclusive Tree Lead: {c_name}</h2>
                        <p><strong>Work:</strong> {ld.get('scope_summary')}</p>
                        <p><strong>Location:</strong> {ld.get('site_address')}</p>
                        <br/>
                        <a href='{checkout.url}' style='background:#2e7d32; color:white; padding:15px 30px; text-decoration:none; border-radius:5px; font-weight:bold; display:inline-block;'>Buy Lead Details (£{amt/100})</a>
                    </div>
                    """
                    requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [sgn["email"]], "subject": f"Lead: {ld.get('site_address')}", "html": email_html}, headers={"Authorization": f"Bearer {R_KEY}"})
                
                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 10: break
        if leads_sent >= 10: break
        
    return {"status": "success", "leads_sent": leads_sent}

@app.post("/webhook", include_in_schema=False)
async def webhook(req: Request):
    sig, payload = req.headers.get("stripe-signature"), await req.body()
    try:
        event = stripe.Webhook.construct_event(payload, sig, S_WH)
        if event["type"] == "checkout.session.completed":
            sess = event["data"]["object"]
            if sess["id"] not in _processed:
                _processed.add(sess["id"])
                m = sess["metadata"]
                requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [T_EM], "subject": "💰 SALE!", "html": f"Paid: {m.get('site_address')}"}, headers={"Authorization": f"Bearer {R_KEY}"})
    except: pass
    return {"status": "ok"}

@app.get("/payment-success", include_in_schema=False)
def success(): return HTMLResponse("<h1>Success!</h1><p>Check your email for the lead details.</p>")

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return HTMLResponse("<h1>Cancelled</h1>")
