import os, json, logging, requests, psycopg2, stripe, urllib3
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Disable SSL warnings for councils using internal/self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="Vector Data Labs - V14.1 Master", docs_url="/docs")
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

# --- THE MASTER LIST (V14.1 Verified Paths) ---
COUNCILS = {
    "Leeds_Control": {
        "type": "arcgis",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Datahub": {
        "type": "rest_api",
        "url": "https://planning.data.london.gov.uk/api/v1/applications/",
        "params": {"page_size": 50}
    },
    "Woking_Surrey": {
        "type": "arcgis",
        "url": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications/FeatureServer/0/query",
        "referer": "https://www.woking.gov.uk/"
    }
}

# --- LOGIC: CLASSIFICATION & DATE HANDLING ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "oak", "ash ", "cedar", "conifer", "birch", "maple", "willow", "sycamore"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "extension", "loft conversion", "demolition"]

def get_d(r):
    v = r.get("received_date") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("DATEAPVAL") or 0
    if isinstance(v, str):
        try: return datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp() * 1000
        except: return 0
    try: return float(v)
    except: return 0

def classify(r):
    p = str(r.get("development_description") or r.get("description") or r.get("PROPOSAL") or r.get("DESCRIPTION") or "").lower()
    if not p: return False, 0
    matches = [k for k in TREE_WORDS if k in p]
    score = len(matches)
    if "tree" in p: score += 2
    if any(x in p for x in ["fell", "remove", "crown", "tpo"]): score += 5
    if any(w in p for w in SKIP_WORDS) and score < 8: return False, 0
    return (score > 2), score

# --- FETCHING LOGIC (Multi-System Support) ---
def fetch_council(name, config):
    h = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    try:
        if config["type"] == "arcgis":
            h["Referer"] = config["referer"]
            q = {"where": "1=1", "outFields": "*", "resultRecordCount": 50, "orderByFields": "OBJECTID DESC", "f": "json"}
            res = requests.get(config["url"], params=q, headers=h, timeout=20, verify=False)
        else:
            res = requests.get(config["url"], params=config["params"], headers=h, timeout=20)

        if res.status_code != 200: return [], f"HTTP {res.status_code} Error"
        if "<html>" in res.text.lower(): return [], "Firewall Block (HTML)"
        
        data = res.json()
        if "error" in data: return [], f"ArcGIS: {data['error'].get('message')}"
        
        if config["type"] == "arcgis":
            return [f.get("attributes", {}) for f in data.get("features", [])], "Success"
        else:
            return data.get("results", []) or data.get("applications", []), "Success"
    except Exception as e:
        return [], f"Fail: {str(e)}"

# --- DATABASE OPERATIONS ---
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
    return f"<html><body style='font-family:sans-serif;text-align:center;'><h1>Vector Data Labs V14.1</h1><p>Leeds: ACTIVE | London: STABILIZING</p><a href='/test-regional'>Diagnostics</a></body></html>"

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
            ref = str(r.get("external_system_reference") or r.get("REFERENCE") or r.get("PLANNO") or r.get("OBJECTID"))
            is_t, _ = classify(r)
            
            if is_t and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
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
                    amt = 6000 if ld.get("high_value") else 3500
                    checkout = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Lead: {ld.get('site_address')}"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(sgn["id"]), "ref": ref, "site_address": ld.get("site_address")}
                    )
                    email_html = f"""
                    <div style='font-family:sans-serif; border-left: 8px solid #1a73e8; padding:20px; background:#f9f9f9;'>
                    <h2 style='color:#1a73e8;'>New Tree Lead: {c_name}</h2>
                    <p><strong>Work:</strong> {ld.get('scope_summary')}</p>
                    <p><strong>Location:</strong> {ld.get('site_address')}</p>
                    <br/>
                    <a href='{checkout.url}' style='background:#1a73e8; color:white; padding:15px 30px; text-decoration:none; border-radius:5px; font-weight:bold; display:inline-block;'>Buy Lead Details (£{amt/100})</a>
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
                requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [T_EM], "subject": "   SALE!", "html": f"Paid: {m.get('site_address')}"}, headers={"Authorization": f"Bearer {R_KEY}"})
    except: pass
    return {"status": "ok"}

@app.get("/payment-success")
def success(): return HTMLResponse("<h1>Success!</h1>")

@app.get("/payment-cancelled")
def cancel(): return HTMLResponse("<h1>Cancelled</h1>")
