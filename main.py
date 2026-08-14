import os, json, logging, requests, psycopg2, stripe, math
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTML_Response
from openai import OpenAI

app = FastAPI(title="Vector Data Labs", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# ENV
OKEY, SURL = os.getenv("OPENAI_API_KEY"), os.getenv("SUPABASE_DB_URL")
S_SEC, S_WH = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY, T_EM = os.getenv("RESEND_API_KEY"), os.getenv("TEST_EMAIL")
T_SEC, P_URL = os.getenv("TRIGGER_SECRET"), os.getenv("PUBLIC_APP_URL")

R_URL, L_URL = "https://api.resend.com/emails", "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query"

client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# --- WEB PAGES FOR STRIPE ---

@app.get("/", response_class=HTML_Response, include_in_schema=False)
def lander():
    return """
    <html><head><title>Vector Data Labs</title><style>body{font-family:sans-serif;line-height:1.6;max-width:800px;margin:auto;padding:50px;color:#333;} h1{color:#2e7d32;} .btn{background:#2e7d32;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;}</style></head>
    <body><h1>Vector Data Labs</h1><p>High-quality, real-time lead generation for UK arboricultural contractors.</p>
    <p>We monitor council planning portals to identify Tree Preservation Order (TPO) and Conservation Area applications, delivering exclusive leads directly to your inbox.</p>
    <p><strong>Contact:</strong> """ + str(T_EM) + """</p>
    <hr/><p style='font-size:12px;'><a href='/terms'>Terms of Service</a> | <a href='/privacy'>Privacy Policy</a></p></body></html>
    """

@app.get("/terms", response_class=HTML_Response, include_in_schema=False)
def terms():
    return "<html><body><h1>Terms of Service</h1><p>Vector Data Labs provides information services. All sales are final. We do not guarantee the accuracy of council data.</p></body></html>"

@app.get("/privacy", response_class=HTML_Response, include_in_schema=False)
def privacy():
    return "<html><body><h1>Privacy Policy</h1><p>We only collect data necessary to process your lead purchases and provide alerts.</p></body></html>"

# --- THE LOGIC ---

TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "conservation"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "conversion"]

def get_d(r):
    v = r.get("DATEAPVAL") or r.get("DATE_RECEIVED") or 0
    return float(v)

def classify(r):
    p = str(r.get("PROPOSAL") or "").lower()
    if not p: return False, 0
    m = [k for k in TREE_WORDS if k in p]
    s = len(m)
    if "tree" in p: s += 2
    if any(x in p for x in ["fell", "remove", "crown"]): s += 5
    if any(w in p for w in SKIP_WORDS) and s < 7: return False, 0
    return (s > 1), s

def fetch_leeds():
    h = {"User-Agent": "Mozilla/5.0"}
    q = {"where": "1=1", "outFields": "*", "resultRecordCount": 500, "orderByFields": "OBJECTID DESC", "f": "json"}
    try:
        res = requests.get(L_URL, params=q, headers=h, timeout=30)
        return [f.get("attributes", {}) for f in res.json().get("features", [])]
    except: return []

def is_already_sent(ref):
    if not SURL: return False
    try:
        conn = psycopg2.connect(SURL)
        with conn.cursor() as cur:
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

@app.get("/trigger-scrape", tags=["Live"])
def scrape(secret: str = Query(...)):
    if secret != T_SEC: raise HTTPException(status_code=401)
    recs = fetch_leeds()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    found = [r for r in recs if get_d(r) >= cutoff and classify(r)[0]]
    if not found: return {"status": "no leads"}
    found.sort(key=lambda x: classify(x)[1], reverse=True)
    
    selected = None
    for r in found:
        if not is_already_sent(r.get("REFVAL")):
            selected = r
            break
    
    if not selected: return {"status": "all sent"}
    
    ai = client.chat.completions.create(
        model="gpt-4o-mini", response_format={"type": "json_object"},
        messages=[{"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)"},
                  {"role": "user", "content": f"Addr: {selected.get('ADDRESS')} Prop: {selected.get('PROPOSAL')}"}]
    )
    ld = json.loads(ai.choices[0].message.content)
    
    # Contractors
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
            metadata={"surgeon_id": str(cn["id"]), "ref": selected.get("REFVAL"), "site_address": ld.get("site_address"), "applicant_name": ld.get("applicant_name")}
        )
        payload = {"from": "Vector Data Labs <onboarding@resend.dev>", "to": [cn["email"]], "subject": "New Lead Found", "html": f"<h3>New Job</h3><p>{ld['scope_summary']}</p><a href='{sess.url}'>Buy Lead</a>"}
        requests.post(R_URL, json=payload, headers={"Authorization": f"Bearer {R_KEY}"})
    
    mark_as_sent(selected.get("REFVAL"))
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
                m = sess["metadata"]
                msg = f"PAID: {m.get('site_address')}"
                requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [T_EM], "subject": "Lead Paid!", "text": msg}, headers={"Authorization": f"Bearer {R_KEY}"})
    except: pass
    return {"status": "ok"}

@app.get("/payment-success", include_in_schema=False)
def success(): return {"message": "Success"}

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return {"message": "Payment Cancelled"}
