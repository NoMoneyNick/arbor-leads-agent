import os, json, logging, requests, psycopg2, stripe
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from openai import OpenAI

app = FastAPI(title="Vector Data Labs", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# ENV
OKEY, SURL = os.getenv("OPENAI_API_KEY"), os.getenv("SUPABASE_DB_URL")
S_SEC, S_WH = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY, T_EM = os.getenv("RESEND_API_KEY"), os.getenv("TEST_EMAIL")
T_SEC, P_URL = os.getenv("TRIGGER_SECRET"), os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
L_URL = "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query"

client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# LOGIC
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "conservation", "birch", "oak", "ash", "sycamore", "willow"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "conversion"]

def get_d(r):
    v = r.get("DATEAPVAL") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or 0
    return float(v)

def classify(r):
    p = str(r.get("PROPOSAL") or "").lower()
    if not p: return False, 0
    m = [k for k in TREE_WORDS if k in p]
    s = len(m)
    if "tree" in p: s += 2
    if any(x in p for x in ["fell", "remove", "crown", "reduce", "thin"]): s += 5
    if any(w in p for w in SKIP_WORDS) and s < 7: return False, 0
    return (s > 1), s

def fetch():
    h = {"User-Agent": "Mozilla/5.0 Chrome/121.0.0.0", "Referer": "https://www.leeds.gov.uk/"}
    q = {"where": "1=1", "outFields": "*", "returnGeometry": "false", "resultRecordCount": 1000, "orderByFields": "OBJECTID DESC", "f": "json"}
    try:
        res = requests.get(L_URL, params=q, headers=h, timeout=30)
        return [f.get("attributes", {}) for f in res.json().get("features", [])]
    except: return []

# --- EMAILS WITH LOGGING ---

def send_email(to, subject, html):
    if not R_KEY:
        logger.error("RESEND_API_KEY IS MISSING IN RENDER!")
        return
    
    payload = {"from": "onboarding@resend.dev", "to": [to], "subject": subject, "html": html}
    headers = {"Authorization": f"Bearer {R_KEY}", "Content-Type": "application/json"}
    
    try:
        r = requests.post(R_URL, json=payload, headers=headers, timeout=10)
        logger.info(f"Resend Response: {r.status_code} - {r.text}")
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Email failed to send: {str(e)}")

def get_lead_alert_html(cn_name, ld, url):
    clean_sum = ld['scope_summary'].replace('\r', '<br/>').replace('\n', '<br/>')
    return f"""<div style="font-family:sans-serif;max-width:600px;margin:auto;border:1px solid #eee;padding:20px;">
    <h2 style="color:#2e7d32;">New Tree Lead</h2><p>Hello {cn_name},</p>
    <div style="background:#f5f5f5;padding:15px;border-radius:5px;">
    <p><strong>Work:</strong><br/>{clean_sum}</p>
    <p><strong>Location:</strong> {ld['postcode']}</p></div>
    <p style="text-align:center;margin-top:25px;">
    <a href="{url}" style="background:#2e7d32;color:white;padding:12px 25px;text-decoration:none;border-radius:5px;font-weight:bold;display:inline-block;">View & Buy Lead</a></p>
    <p style="font-size:11px;color:#999;margin-top:20px;">Vector Data Labs - Leeds Council Planning</p></div>"""

def get_unlock_html(m):
    return f"""<div style="font-family:sans-serif;max-width:600px;margin:auto;border:1px solid #eee;padding:20px;">
    <h2 style="color:#1565c0;">Lead Unlocked</h2>
    <div style="border:1px solid #1565c0;padding:15px;border-radius:5px;">
    <p><strong>Address:</strong><br/>{m.get('site_address')}</p>
    <p><strong>Applicant:</strong><br/>{m.get('applicant_name') or 'Check Council Portal'}</p>
    <p><strong>Ref:</strong> {m.get('ref')}</p></div></div>"""

# --- ROUTES ---

@app.get("/", include_in_schema=False)
def home(): return {"message": "Active. Go to /docs"}

@app.get("/test-leeds", tags=["Testing"])
def test():
    recs = fetch()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).timestamp() * 1000
    leads = []
    for r in recs:
        is_t, s = classify(r)
        if is_t and get_d(r) >= cutoff:
            r["_score"] = s
            leads.append(r)
    return {"scanned": len(recs), "found": len(leads), "leads": leads[:20]}

@app.get("/trigger-scrape", tags=["Live"])
def scrape(secret: str = Query(..., description="Your secret key")):
    if secret != T_SEC: raise HTTPException(status_code=401)
    recs = fetch()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    found = [r for r in recs if get_d(r) >= cutoff and classify(r)[0]]
    if not found: return {"status": "no leads found in last 30 days"}
    found.sort(key=lambda x: classify(x)[1], reverse=True)
    best = found[0]
    
    ai = client.chat.completions.create(
        model="gpt-4o-mini", response_format={"type": "json_object"},
        messages=[{"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)"},
                  {"role": "user", "content": f"Addr: {best.get('ADDRESS')} Prop: {best.get('PROPOSAL')}"}]
    )
    ld = json.loads(ai.choices[0].message.content)
    
    cons = []
    if SURL:
        try:
            db = psycopg2.connect(SURL)
            with db.cursor() as c:
                c.execute("SELECT id, business_name, email FROM tree_surgeons WHERE active IS TRUE")
                for row in c.fetchall(): cons.append({"id": row[0], "name": row[1], "email": row[2]})
            db.close()
        except: pass
    if not cons: cons.append({"id": 1, "name": "Test User", "email": T_EM})
    
    for cn in cons:
        amt = 4500 if ld.get("high_value") else 2500
        sess = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": "Exclusive Tree Lead"}, "unit_amount": amt}, "quantity": 1}],
            mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
            metadata={"surgeon_id": str(cn["id"]), "ref": best.get("REFVAL", ""), "site_address": ld.get("site_address", ""), "applicant_name": ld.get("applicant_name", "")}
        )
        html = get_lead_alert_html(cn["name"], ld, sess.url)
        send_email(cn["email"], f"New Tree Job: {ld.get('postcode')}", html)
    return {"status": "sent", "address": ld["site_address"]}

@app.post("/webhook", include_in_schema=False)
async def webhook(req: Request):
    sig, payload = req.headers.get("stripe-signature"), await req.body()
    try:
        event = stripe.Webhook.construct_event(payload, sig, S_WH)
        if event["type"] == "checkout.session.completed":
            sess = event["data"]["object"]
            if sess["id"] not in _processed:
                _processed.add(sess["id"])
                html = get_unlock_html(sess["metadata"])
                send_email(T_EM, "Lead Unlocked - Site Details", html)
    except: pass
    return {"status": "ok"}

@app.get("/payment-success", include_in_schema=False)
def success(): return {"message": "Success"}

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return {"message": "Cancelled"}
