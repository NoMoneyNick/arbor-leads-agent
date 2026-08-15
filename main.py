import os, json, logging, requests, psycopg2, stripe
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Standard logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

app = FastAPI(title="Vector Data Labs - V17.2 Master", docs_url="/docs")

# --- ENVIRONMENT VARIABLES ---
OKEY, SURL = os.getenv("OPENAI_API_KEY"), os.getenv("SUPABASE_DB_URL")
S_SEC, S_WH = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY, T_EM = os.getenv("RESEND_API_KEY"), os.getenv("TEST_EMAIL")
T_SEC, P_URL = os.getenv("TRIGGER_SECRET"), os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# --- OFFICIAL ENDPOINT CONFIGURATION ---
# These are the direct, publicly available ArcGIS FeatureServer links used by the councils.
COUNCILS = {
    "Leeds_City": {
        "type": "arcgis",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Mega_Hub": {
        "type": "arcgis",
        # Direct public ArcGIS link for the GLA Planning Datahub
        "url": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_London_Datahub/FeatureServer/0/query",
        "referer": "https://www.london.gov.uk/"
    },
    "Woking_Surrey": {
        "type": "arcgis",
        # Corrected service name for the shared Surrey/London cluster
        "url": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications/FeatureServer/0/query",
        "referer": "https://www.woking.gov.uk/"
    }
}

# --- LOGIC: CLASSIFICATION ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "oak", "ash", "cedar", "conifer", "birch", "maple", "willow", "sycamore"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "extension", "loft conversion", "demolition"]

def get_d(r):
    v = r.get("received_date") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("DATEAPVAL") or 0
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
    if any(x in p for x in ["fell", "remove", "crown", "tpo"]): score += 5
    
    if any(w in p for w in SKIP_WORDS) and score < 8: return False, 0
    return (score > 2), score

# --- FETCHING LOGIC ---
def fetch_data(name, config):
    h = {
        "User-Agent": "VectorDataLabs/1.2 (Official Public Data Integration)",
        "Accept": "application/json"
    }
    try:
        if config["type"] == "arcgis":
            h["Referer"] = config["referer"]
            q = {
                "where": "1=1",
                "outFields": "*",
                "resultRecordCount": 50,
                "orderByFields": "OBJECTID DESC",
                "f": "json"
            }
            res = requests.get(config["url"], params=q, headers=h, timeout=25)
        else:
            res = requests.get(config["url"], params=config.get("params", {}), headers=h, timeout=25)

        if res.status_code != 200:
            return [], f"HTTP {res.status_code}"
        
        data = res.json()
        if "error" in data:
            return [], f"ArcGIS Error: {data['error'].get('message')}"
            
        if config["type"] == "arcgis":
            return [f.get("attributes", {}) for f in data.get("features", [])], "Online"
        else:
            return data.get("results", []) or data.get("applications", []), "Online"
            
    except Exception as e:
        return [], f"Fail: {str(e)}"

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
    <html><body style='font-family:sans-serif;text-align:center;padding-top:50px; background:#fafafa;'>
    <div style='display:inline-block; padding:40px; background:white; border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,0.05); border-top: 5px solid #1a73e8;'>
    <h1>Vector Data Labs V17.2</h1>
    <p>Leeds: <b>ONLINE</b> | London/Surrey: <b>CONNECTING</b></p>
    <hr style='border:0; border-top:1px solid #eee; margin:20px 0;'/>
    <a href='/test-regional' style='color:#1a73e8; text-decoration:none; font-weight:bold;'>Run Regional Health Check</a>
    </div>
    </body></html>
    """

@app.get("/test-regional")
def test_all():
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_data(name, config)
        found = [r for r in recs if classify(r)[0]]
        results[name] = {"status": status, "scanned": len(recs), "tree_leads": len(found)}
    return results

@app.get("/trigger-scrape")
def scrape(secret: str = Query(...)):
    if secret != T_SEC: raise HTTPException(status_code=401)
    leads_sent = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    
    for c_name, config in COUNCILS.items():
        recs, _ = fetch_data(c_name, config)
        for r in recs:
            # Multi-API Reference Generator
            ref = str(r.get("external_system_reference") or r.get("REFERENCE") or r.get("OBJECTID"))
            is_valid, _ = classify(r)
            
            if is_valid and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
                addr = r.get('full_address') or r.get('ADDRESS') or r.get('LOCATION')
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
                    email_html = f"<h2>New Tree Lead: {c_name}</h2><p>{ld.get('scope_summary')}</p><a href='{checkout.url}'>Purchase Lead Details</a>"
                    requests.post(R_URL, json={"from": "Vector Data Labs <leads@resend.dev>", "to": [sgn["email"]], "subject": f"Lead: {ld.get('site_address')}", "html": email_html}, headers={"Authorization": f"Bearer {R_KEY}"})
                
                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 10: break
        if leads_sent >= 10: break
    return {"status": "success", "leads_sent": leads_sent}

@app.get("/payment-success", include_in_schema=False)
def success(): return HTMLResponse("<h1>Success!</h1>")

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return HTMLResponse("<h1>Cancelled</h1>")
