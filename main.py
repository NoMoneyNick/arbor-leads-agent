import os, json, logging, requests, psycopg2, stripe
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

app = FastAPI(title="Vector Data Labs - Surrey Cloud Test", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# ENV
OKEY, SURL = os.getenv("OPENAI_API_KEY"), os.getenv("SUPABASE_DB_URL")
S_SEC, S_WH = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY, T_EM = os.getenv("RESEND_API_KEY"), os.getenv("TEST_EMAIL")
T_SEC, P_URL = os.getenv("TRIGGER_SECRET"), os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# --- THE CLOUD ENDPOINTS (The "Back Door") ---
COUNCILS = {
    "Leeds": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
    "Woking": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications_Live/FeatureServer/0/query",
    "Surrey Heath": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications/FeatureServer/0/query"
}

# --- WEB PAGES ---
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def lander():
    return f"<html><body><h1>Vector Data Labs</h1><p>System Active.</p><p>Contact: {T_EM}</p><hr/><a href='/terms'>Terms</a> | <a href='/privacy'>Privacy</a></body></html>"

@app.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def terms(): return "<html><body><h1>Terms</h1><p>All sales final.</p></body></html>"

@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy(): return "<html><body><h1>Privacy</h1><p>Data used for lead processing only.</p></body></html>"

# --- LOGIC ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "conservation"]
SKIP_WORDS = ["dwelling", "erection of", "new build"]

def get_d(r):
    v = r.get("DATEAPVAL") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or 0
    return float(v)

def classify(r):
    # Councils use PROPOSAL or DESCRIPTION
    p = str(r.get("PROPOSAL") or r.get("DESCRIPTION") or r.get("DESCRIPT") or "").lower()
    if not p: return False, 0
    m = [k for k in TREE_WORDS if k in p]
    s = len(m)
    if "tree" in p: s += 2
    if any(x in p for x in ["fell", "remove", "crown"]): s += 5
    if any(w in p for w in SKIP_WORDS) and s < 7: return False, 0
    return (s > 1), s

def fetch_council(url):
    h = {"User-Agent": "Mozilla/5.0"}
    q = {"where": "1=1", "outFields": "*", "resultRecordCount": 100, "orderByFields": "OBJECTID DESC", "f": "json"}
    try:
        res = requests.get(url, params=q, headers=h, timeout=15)
        data = res.json()
        if "error" in data: return [], f"Error: {data['error'].get('message')}"
        return [f.get("attributes", {}) for f in data.get("features", [])], "Success"
    except Exception as e:
        return [], str(e)

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

@app.get("/test-regional", tags=["Diagnostics"])
def test_all():
    results = {}
    for name, url in COUNCILS.items():
        recs, status = fetch_council(url)
        found = [r for r in recs if classify(r)[0]]
        results[name] = {"status": status, "scanned": len(recs), "tree_leads": len(found)}
    return results

@app.get("/trigger-scrape", tags=["Live"])
def scrape(secret: str = Query(...)):
    if secret != T_SEC: raise HTTPException(status_code=401)
    leads_sent = 0
    for c_name, c_url in COUNCILS.items():
        recs, _ = fetch_council(c_url)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        for r in recs:
            ref = r.get("REFVAL") or r.get("REFERENCE") or str(r.get("OBJECTID"))
            is_t, s = classify(r)
            if is_t and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
                # AI
                addr = r.get('ADDRESS') or r.get('LOCATION') or r.get('SITE_ADDRESS')
                prop = r.get('PROPOSAL') or r.get('DESCRIPTION') or r.get('DESCRIPT')
                ai = client.chat.completions.create(
                    model="gpt-4o-mini", response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)"},
                              {"role": "user", "content": f"Addr: {addr} Prop: {prop}"}]
                )
                ld = json.loads(ai.choices[0].message.content)
                cons = []
                if SURL:
                    try:
                        db = psycopg2.connect(SURL); c = db.cursor()
                        c.execute("SELECT id, email FROM tree_surgeons WHERE active IS TRUE")
                        for row in c.fetchall(): cons.append({"id": row[0], "email": row[1]})
                        db.close()
                    except: pass
                if not cons: cons.append({"id": 1, "email": T_EM})
                for cn in cons:
                    amt = 4500 if ld.get("high_value") else 2500
                    sess = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": "Exclusive Tree Lead"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(cn["id"]), "ref": ref, "site_address": ld.get("site_address"), "applicant_name": ld.get("applicant_name")}
                    )
                    payload = {"from": "Vector Data Labs <onboarding@resend.dev>", "to": [cn["email"]], "subject": f"New Lead: {c_name}", "html": f"<h3>New Tree Lead</h3><p>{ld['scope_summary']}</p><a href='{sess.url}'>Buy Lead</a>"}
                    requests.post(R_URL, json=payload, headers={"Authorization": f"Bearer {R_KEY}"})
                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 3: break
        if leads_sent >= 3: break
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
                msg = f"<h3>Lead Paid!</h3><p>Address: {m.get('site_address')}</p>"
                requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [T_EM], "subject": "Paid!", "html": msg}, headers={"Authorization": f"Bearer {R_KEY}"})
    except: pass
    return {"status": "ok"}

@app.get("/payment-success", include_in_schema=False)
def success(): return {"message": "Payment Successful"}

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return {"message": "Payment Cancelled"}
