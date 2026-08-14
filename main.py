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

R_URL, L_URL = "https://api.resend.com/emails", "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query"

client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "conservation", "birch", "oak", "ash", "sycamore"]
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
    if any(x in p for x in ["fell", "remove", "crown", "thin", "reduce"]): s += 5
    if any(w in p for w in SKIP_WORDS) and s < 7: return False, 0
    return (s > 1), s

def fetch_leeds():
    h = {"User-Agent": "Mozilla/5.0 Chrome/121.0.0.0", "Referer": "https://www.leeds.gov.uk/"}
    q = {"where": "1=1", "outFields": "REFVAL,ADDRESS,PROPOSAL,DATEAPVAL,OBJECTID", "resultRecordCount": 1000, "orderByFields": "OBJECTID DESC", "f": "json"}
    try:
        res = requests.get(L_URL, params=q, headers=h, timeout=30)
        return [f.get("attributes", {}) for f in res.json().get("features", [])]
    except: return []

# --- DATABASE HELPERS ---

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
        conn = psycopg2.connect(SURL)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sent_leads (ref) VALUES (%s) ON CONFLICT DO NOTHING", (ref,))
        conn.commit()
        conn.close()
    except: pass

# --- EMAILS ---

def send_email(to, subject, html):
    payload = {"from": "Vector Data Labs <onboarding@resend.dev>", "to": [to], "subject": subject, "html": html}
    requests.post(R_URL, json=payload, headers={"Authorization": f"Bearer {R_KEY}", "Content-Type": "application/json"})

def get_lead_alert_html(cn_name, ld, url):
    clean_summary = ld['scope_summary'].replace('\r', '<br/>').replace('\n', '<br/>')
    return f"""<div style="font-family:Arial;max-width:600px;margin:auto;border:1px solid #ddd;border-radius:10px;overflow:hidden;">
    <div style="background:#2e7d32;padding:20px;text-align:center;color:white;"><h2 style="margin:0;">New Exclusive Tree Lead</h2></div>
    <div style="padding:30px;"><p>Hello {cn_name}, a new job is available:</p>
    <div style="background:#f9f9f9;padding:15px;border-radius:5px;border:1px solid #eee;">
    <p><strong>Work:</strong><br/>{clean_summary}</p><p><strong>Location:</strong> {ld['postcode']}</p></div>
    <div style="text-align:center;margin:30px 0;"><a href="{url}" style="background:#2e7d32;color:white;padding:15px 30px;text-decoration:none;border-radius:5px;font-weight:bold;font-size:18px;">Buy Lead Now</a></div>
    </div></div>"""

# --- ROUTES ---

@app.get("/test-leeds", tags=["Diagnostics"])
def test():
    recs = fetch_leeds()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=120)).timestamp() * 1000
    leads = []
    for r in recs:
        is_t, s = classify(r)
        if is_t and get_d(r) >= cutoff:
            r["_already_sent"] = is_already_sent(r.get("REFVAL"))
            r["_score"] = s
            leads.append(r)
    return {"total": len(recs), "found": len(leads), "leads": leads[:20]}

@app.get("/trigger-scrape", tags=["Live"])
def scrape(secret: str = Query(..., description="TRIGGER_SECRET")):
    if secret != T_SEC: raise HTTPException(status_code=401)
    recs = fetch_leeds()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    
    # 1. Filter and Score
    found = []
    for r in recs:
        is_t, s = classify(r)
        if is_t and get_d(r) >= cutoff:
            found.append({"score": s, "data": r})
    
    if not found: return {"status": "no new leads"}
    
    # 2. Sort by score
    found.sort(key=lambda x: x["score"], reverse=True)
    
    # 3. Find the best lead we HAVEN'T sent yet
    selected = None
    for f in found:
        ref = f["data"].get("REFVAL")
        if not is_already_sent(ref):
            selected = f["data"]
            break
            
    if not selected: return {"status": "all current leads already sent"}

    # 4. AI Process
    ai = client.chat.completions.create(
        model="gpt-4o-mini", response_format={"type": "json_object"},
        messages=[{"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)"},
                  {"role": "user", "content": f"Addr: {selected.get('ADDRESS')} Prop: {selected.get('PROPOSAL')}"}]
    )
    ld = json.loads(ai.choices[0].message.content)
    
    # 5. Get Contractors
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
    
    # 6. Send
    for cn in cons:
        amt = 4500 if ld.get("high_value") else 2500
        sess = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": "Exclusive Tree Lead"}, "unit_amount": amt}, "quantity": 1}],
            mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
            metadata={"surgeon_id": str(cn["id"]), "ref": selected.get("REFVAL"), "site_address": ld.get("site_address"), "applicant_name": ld.get("applicant_name")}
        )
        send_email(cn["email"], "New Tree Job Opportunity", get_lead_alert_html(cn["name"], ld, sess.url))
    
    mark_as_sent(selected.get("REFVAL"))
    return {"status": "success", "sent_ref": selected.get("REFVAL")}

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
                html = f"<h2>Lead Unlocked</h2><p>Address: {m.get('site_address')}</p><p>Name: {m.get('applicant_name')}</p>"
                send_email(T_EM, "Lead Paid!", html)
    except: pass
    return {"status": "ok"}

@app.get("/payment-success", include_in_schema=False)
def success(): return {"message": "Payment Successful"}

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return {"message": "Payment Cancelled"}
