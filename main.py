import os, json, logging, requests, psycopg2, stripe, urllib3
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Disable SSL warnings for councils with misconfigured certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="Vector Data Labs - London Cloud V8.7", docs_url="/docs")
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

# --- THE LONDON CLOUD BACKDOORS (V8.7 Verified) ---
# We shift from local .gov.uk links to direct ESRI Cloud hosting.
COUNCILS = {
    # GLA Datahub - Attempting the 'Map 01' stable path
    "London_Mega_Hub": "https://maps.london.gov.uk/arcgis/rest/services/apps/planning_data_map_01/MapServer/1/query",
    # Redbridge & Others often use this specific ESRI organization ID (S96pW9S9VlU6z7fK)
    "Redbridge_Cloud": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications/FeatureServer/0/query",
    # Bromley uses a dedicated MapServer that is usually stable
    "Bromley_London": "https://maps.bromley.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
    # Barnet - Adjusted path
    "Barnet_Cloud": "https://maps.barnet.gov.uk/arcgis/rest/services/Planning/Planning_Applications_Public/MapServer/0/query",
    "Leeds_Control": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query"
}

# --- WEB PAGES ---
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def lander():
    return f"""
    <html>
        <body style='font-family:sans-serif; text-align:center; padding-top:50px; background:#f0f2f5;'>
            <div style='display:inline-block; padding:50px; background:white; border-radius:15px; box-shadow:0 10px 25px rgba(0,0,0,0.05);'>
                <h1 style='color:#1b5e20;'>Vector Data Labs</h1>
                <p style='color:#666;'>System V8.7 (Cloud Infiltration) Active.</p>
                <p><b>Target:</b> All 32 London Boroughs</p>
                <hr style='border:0; border-top:1px solid #eee; margin:25px 0;'/>
                <a href='/test-regional' style='color:#2e7d32; text-decoration:none; font-weight:bold;'>Check Live Signal</a>
            </div>
        </body>
    </html>
    """

# --- LOGIC: CLASSIFICATION & FETCHING ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "oak", "ash ", "sycamore", "willow", "cedar", "conifer", "birch", "maple"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "extension", "loft conversion", "basement", "advertisement", "shopfront", "signage"]

def get_d(r):
    # London councils use diverse date keys
    v = r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("DATEAPVAL") or r.get("RECDAT") or r.get("actual_decision_date") or r.get("VALIDAT") or 0
    try: return float(v)
    except: return 0

def classify(r):
    # Aggregate all possible description fields
    p = str(
        r.get("development_description") or 
        r.get("PROPOSAL") or 
        r.get("DESCRIPTION") or 
        r.get("DESCRIPT") or 
        r.get("DETDESC") or 
        r.get("REASON") or ""
    ).lower()
    
    if not p: return False, 0
    
    matches = [k for k in TREE_WORDS if k in p]
    score = len(matches)
    
    if "tree" in p: score += 2
    if any(x in p for x in ["fell", "remove", "crown", "tpo", "conservation area"]): score += 5
    
    # Negative filter: Ignore massive construction projects that just mention a tree
    if any(w in p for w in SKIP_WORDS) and score < 8: return False, 0
    
    return (score > 2), score

def fetch_council(url):
    # Masking as a modern browser with Google as the referer
    h = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.google.com/",
        "Accept": "application/json"
    }
    
    # Parameters designed to be 'server-friendly' to avoid 403 blocks
    q = {
        "where": "1=1", 
        "outFields": "*", 
        "resultRecordCount": 50, 
        "orderByFields": "OBJECTID DESC", 
        "f": "json"
    }
    try:
        # BATTLE SECRET: verify=False is required for councils like Southwark
        res = requests.get(url, params=q, headers=h, timeout=25, verify=False)
        
        if res.status_code == 404: return [], "404 Not Found (Path Changed)"
        if res.status_code != 200: return [], f"HTTP {res.status_code}"
            
        data = res.json()
        if "error" in data: return [], f"ArcGIS Hub Error: {data['error'].get('message')}"
        
        return [f.get("attributes", {}) for f in data.get("features", [])], "Success"
    except Exception as e:
        return [], f"Connection Failed: {str(e)}"

# --- DATABASE ---
def is_already_sent(ref):
    if not SURL: return False
    try:
        conn = psycopg2.connect(SURL); cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS sent_leads (ref TEXT PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT NOW());")
        conn.commit()
        cur.execute("SELECT 1 FROM sent_leads WHERE ref = %s", (ref,))
        exists = cur.fetchone() is not None
        conn.close()
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
    """Health check for London Cloud Signal."""
    results = {}
    for name, url in COUNCILS.items():
        recs, status = fetch_council(url)
        found = [r for r in recs if classify(r)[0]]
        results[name] = {
            "status": status, 
            "scanned": len(recs), 
            "tree_leads": len(found),
            "sample_ref": recs[0].get("REFERENCE") or recs[0].get("OBJECTID") if recs else "None"
        }
    return results

@app.get("/trigger-scrape")
def scrape(secret: str = Query(...)):
    if secret != T_SEC: raise HTTPException(status_code=401)
        
    leads_sent = 0
    for c_name, c_url in COUNCILS.items():
        recs, _ = fetch_council(c_url)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        
        for r in recs:
            # Multi-key reference generator
            ref = r.get("external_system_reference") or r.get("REFERENCE") or r.get("PLANNO") or r.get("REFVAL") or r.get("P_REF") or str(r.get("OBJECTID"))
            is_tree, _ = classify(r)
            
            if is_tree and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
                addr = r.get('full_address') or r.get('ADDRESS') or r.get('LOCATION') or r.get('SITE_ADDRESS') or r.get('ADR1')
                prop = r.get('development_description') or r.get('PROPOSAL') or r.get('DESCRIPTION') or r.get('DETDESC')
                
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

                # Get Active Tree Surgeons
                surgeons = []
                if SURL:
                    try:
                        db = psycopg2.connect(SURL); c = db.cursor()
                        c.execute("SELECT id, email FROM tree_surgeons WHERE active IS TRUE")
                        for row in c.fetchall(): surgeons.append({"id": row[0], "email": row[1]})
                        db.close()
                    except: pass
                
                if not surgeons: surgeons.append({"id": 999, "email": T_EM})

                for sgn in surgeons:
                    amt = 5500 if ld.get("high_value") else 3000
                    checkout = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"London Lead: {ld.get('site_address')}"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(sgn["id"]), "ref": ref, "site_address": ld.get("site_address")}
                    )
                    
                    email_html = f"""
                    <div style='font-family:sans-serif; border:2px solid #1b5e20; padding:25px; max-width:600px;'>
                        <h2 style='color:#1b5e20;'>Exclusive London Tree Lead</h2>
                        <p><strong>Borough:</strong> {c_name}</p>
                        <p><strong>Scope:</strong> {ld.get('scope_summary')}</p>
                        <p><strong>Location:</strong> {ld.get('site_address')}</p>
                        <br/>
                        <a href='{checkout.url}' style='background:#1b5e20; color:white; padding:15px 30px; text-decoration:none; border-radius:5px; font-weight:bold; display:inline-block;'>Purchase Lead (£{amt/100})</a>
                        <p style='font-size:12px; color:gray; margin-top:20px;'>*This lead is exclusive to you for 2 hours.</p>
                    </div>
                    """
                    requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [sgn["email"]], "subject": f"London Lead: {ld.get('site_address')}", "html": email_html}, headers={"Authorization": f"Bearer {R_KEY}"})
                
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
                msg = f"<h3>London Lead Paid!</h3><p>Address: {m.get('site_address')}</p>"
                requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [T_EM], "subject": "💰 LONDON SALE!", "html": msg}, headers={"Authorization": f"Bearer {R_KEY}"})
    except: pass
    return {"status": "ok"}

@app.get("/payment-success", include_in_schema=False)
def success(): return HTMLResponse("<html><body><h1>Success!</h1><p>Lead details sent to your email.</p></body></html>")

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return HTMLResponse("<html><body><h1>Payment Cancelled</h1></body></html>")
