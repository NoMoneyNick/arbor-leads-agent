import os, json, logging, requests, psycopg2, stripe
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

app = FastAPI(title="Vector Data Labs - London Siege V8.5", docs_url="/docs")
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

# --- THE LONDON TARGET LIST ---
# If the Hub fails, the individual Boroughs will catch the leads.
COUNCILS = {
    "London_Mega_Hub": "https://maps.london.gov.uk/arcgis/rest/services/planning/Planning_London_Datahub/MapServer/0/query",
    "Southwark_London": "https://geo.southwark.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
    "Barnet_London": "https://maps.barnet.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
    "Croydon_London": "https://maps.croydon.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
    "Leeds_Control": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query"
}

# --- WEB PAGES ---
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def lander():
    return f"""
    <html>
        <body style='font-family:sans-serif; text-align:center; padding-top:50px; background:#f4f4f9;'>
            <div style='display:inline-block; padding:40px; background:white; border-radius:10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
                <h1 style='color:#1a73e8;'>Vector Data Labs</h1>
                <p style='color:#5f6368;'>System V8.5 (London Siege) Active.</p>
                <p>Status: Monitoring 35+ London Authorities</p>
                <hr style='border:0; border-top:1px solid #eee; margin:20px 0;'/>
                <a href='/test-regional' style='color:#1a73e8; text-decoration:none;'>Diagnostics</a>
            </div>
        </body>
    </html>
    """

# --- LOGIC: CLASSIFICATION & FETCHING ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "oak", "ash ", "sycamore", "willow"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "extension", "loft conversion", "basement", "advertisement"]

def get_d(r):
    # Extracts timestamp from various possible ArcGIS date fields
    v = r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("DATEAPVAL") or r.get("RECDAT") or r.get("actual_decision_date") or 0
    try: return float(v)
    except: return 0

def classify(r):
    # Aggregated descriptions from different council schemas
    p = str(
        r.get("development_description") or 
        r.get("PROPOSAL") or 
        r.get("DESCRIPTION") or 
        r.get("DESCRIPT") or 
        r.get("DETDESC") or ""
    ).lower()
    
    if not p: return False, 0
    
    m = [k for k in TREE_WORDS if k in p]
    s = len(m)
    
    if "tree" in p: s += 2
    if any(x in p for x in ["fell", "remove", "crown", "tpo", "conservation area"]): s += 5
    
    # Negative filtering: Avoid major construction unless it's a very clear tree lead
    if any(w in p for w in SKIP_WORDS) and s < 8: return False, 0
    
    return (s > 2), s

def fetch_council(url):
    # Stronger Browser Masking
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.google.com/"
    }
    
    q = {
        "where": "1=1", 
        "outFields": "*", 
        "resultRecordCount": 50, 
        "orderByFields": "OBJECTID DESC", 
        "f": "json"
    }
    try:
        res = requests.get(url, params=q, headers=h, timeout=25)
        # Check if the server actually returned JSON
        if res.status_code != 200:
            return [], f"HTTP Error {res.status_code}"
            
        data = res.json()
        if "error" in data: 
            return [], f"ArcGIS Error: {data['error'].get('message')}"
        
        return [f.get("attributes", {}) for f in data.get("features", [])], "Success"
    except Exception as e:
        return [], f"Fetch Exception: {str(e)}"

# --- DATABASE OPERATIONS ---
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
    """Scoreboard to verify London connectivity."""
    results = {}
    for name, url in COUNCILS.items():
        recs, status = fetch_council(url)
        found = [r for r in recs if classify(r)[0]]
        results[name] = {
            "status": status, 
            "scanned": len(recs), 
            "tree_leads": len(found),
            "raw_log": str(recs[0])[:200] if recs else "None"
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
            ref = r.get("external_system_reference") or r.get("REFERENCE") or r.get("PLANNO") or r.get("P_REF") or str(r.get("OBJECTID"))
            is_t, score = classify(r)
            
            if is_t and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
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

                # Get Active Surgeons
                cons = []
                if SURL:
                    try:
                        db = psycopg2.connect(SURL); c = db.cursor()
                        c.execute("SELECT id, email FROM tree_surgeons WHERE active IS TRUE")
                        for row in c.fetchall(): cons.append({"id": row[0], "email": row[1]})
                        db.close()
                    except: pass
                
                if not cons: cons.append({"id": 999, "email": T_EM})

                for cn in cons:
                    amt = 5500 if ld.get("high_value") else 3000
                    sess = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"London Tree Lead: {ld.get('site_address')}"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", 
                        success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(cn["id"]), "ref": ref, "site_address": ld.get("site_address")}
                    )
                    
                    email_body = f"""
                    <div style='font-family:sans-serif; border:1px solid #eee; padding:20px;'>
                        <h2 style='color:#2e7d32;'>New London Tree Lead</h2>
                        <p><strong>Council:</strong> {c_name}</p>
                        <p><strong>Work:</strong> {ld.get('scope_summary')}</p>
                        <p><strong>Location:</strong> {ld.get('site_address')}</p>
                        <br/>
                        <a href='{sess.url}' style='background:#2e7d32; color:white; padding:12px 25px; text-decoration:none; border-radius:5px;'>Buy Lead for £{amt/100}</a>
                    </div>
                    """
                    requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [cn["email"]], "subject": f"Lead: {ld.get('site_address')}", "html": email_body}, headers={"Authorization": f"Bearer {R_KEY}"})
                
                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 10: break
        if leads_sent >= 10: break
        
    return {"status": "success", "leads_processed": leads_sent}

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
                requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [T_EM], "subject": "💰 SALE!", "html": msg}, headers={"Authorization": f"Bearer {R_KEY}"})
    except: pass
    return {"status": "ok"}

@app.get("/payment-success", include_in_schema=False)
def success(): return HTMLResponse("<html><body><h1>Success!</h1><p>Details sent to email.</p></body></html>")

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return HTMLResponse("<html><body><h1>Cancelled</h1></body></html>")
