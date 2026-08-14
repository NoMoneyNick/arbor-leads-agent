import os, json, logging, requests, psycopg2, stripe, urllib3
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Silence SSL warnings for councils using internal/self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="Vector Data Labs - V9.0 London Explorer", docs_url="/docs")
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

# --- THE LONDON DISCOVERY LIST (V9.0 Migrated Paths) ---
# We are targeting the 'Live' and 'Public' mirrors which are usually more stable.
COUNCILS = {
    "Leeds_Control": {
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Mega_Hub": {
        "url": "https://maps.london.gov.uk/arcgis/rest/services/apps/planning_data_map_01/MapServer/1/query",
        "referer": "https://www.london.gov.uk/"
    },
    "Barnet_London": {
        "url": "https://maps.barnet.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
        "referer": "https://www.barnet.gov.uk/"
    },
    "Southwark_London": {
        "url": "https://geo.southwark.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
        "referer": "https://www.southwark.gov.uk/"
    },
    "Wandsworth_Richmond": {
        "url": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications_Live/FeatureServer/0/query",
        "referer": "https://www.wandsworth.gov.uk/"
    }
}

# --- WEB PAGES ---
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def lander():
    return f"""
    <html>
        <body style='font-family:sans-serif; text-align:center; padding-top:50px; background:#f4f7f6;'>
            <div style='display:inline-block; padding:40px; background:white; border-radius:12px; box-shadow:0 8px 16px rgba(0,0,0,0.1); border-top: 5px solid #1a73e8;'>
                <h1 style='color:#1a73e8; margin:0;'>Vector Data Labs</h1>
                <p style='color:#666;'>System V9.0 (London Explorer) Active.</p>
                <hr style='border:0; border-top:1px solid #eee; margin:20px 0;'/>
                <a href='/test-regional' style='color:#1a73e8; text-decoration:none; font-weight:bold;'>Run London Health Check</a>
            </div>
        </body>
    </html>
    """

# --- LOGIC: CLASSIFICATION ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "oak", "ash ", "cedar", "conifer", "birch", "maple", "willow"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "extension", "loft conversion", "basement"]

def get_d(r):
    v = r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("DATEAPVAL") or r.get("RECDAT") or 0
    try: return float(v)
    except: return 0

def classify(r):
    # Search all possible description fields (London Hub uses 'development_description')
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
    if any(x in p for x in ["fell", "remove", "crown", "tpo"]): score += 5
    
    # Strict filter for London: don't buy extension leads
    if any(w in p for w in SKIP_WORDS) and score < 8: return False, 0
    
    return (score > 2), score

# --- LOGIC: FETCHING ---
def fetch_council(name, config):
    url = config["url"]
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": config["referer"],
        "Accept": "application/json"
    }
    
    q = {
        "where": "1=1", 
        "outFields": "*", 
        "resultRecordCount": 50, 
        "orderByFields": "OBJECTID DESC", 
        "f": "json"
    }
    
    try:
        res = requests.get(url, params=q, headers=h, timeout=15, verify=False)
        
        if res.status_code == 404:
            return [], "404: Path Not Found"
            
        data = res.json()
        
        # If ArcGIS returns an error, we report it directly
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
    """Diagnostic scoreboard."""
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_council(name, config)
        found = [r for r in recs if classify(r)[0]]
        results[name] = {
            "status": status, 
            "scanned": len(recs), 
            "tree_leads": len(found),
            "sample_ref": recs[0].get("REFERENCE") or recs[0].get("OBJECTID") if recs else None
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

                # Get Surgeons
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
                    amt = 5500 if ld.get("high_value") else 3000
                    checkout = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Lead: {ld.get('site_address')}"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(sgn["id"]), "ref": ref, "site_address": ld.get("site_address")}
                    )
                    
                    email_html = f"""
                    <div style='font-family:sans-serif; border:2px solid #1a73e8; padding:20px;'>
                        <h2 style='color:#1a73e8;'>New Tree Lead: {c_name}</h2>
                        <p><strong>Work:</strong> {ld.get('scope_summary')}</p>
                        <p><strong>Location:</strong> {ld.get('site_address')}</p>
                        <br/>
                        <a href='{checkout.url}' style='background:#1a73e8; color:white; padding:12px 25px; text-decoration:none; border-radius:5px; font-weight:bold; display:inline-block;'>Buy Lead Details (£{amt/100})</a>
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
def success(): return HTMLResponse("<h1>Success!</h1>")

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return HTMLResponse("<h1>Cancelled</h1>")
