import os, json, logging, requests, psycopg2, stripe
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from openai import OpenAI

app = FastAPI(title="Vector Data Labs - Surrey Engine", docs_url="/docs")
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

# --- THE SURREY & LEEDS LIST ---
COUNCILS = {
    "Leeds": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
    "Woking": "https://maps.woking.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
    "Waverley": "https://planningit.waverley.gov.uk/arcgis/rest/services/Planning/Planning_Applications/MapServer/0/query",
    "Elmbridge": "https://maps.elmbridge.gov.uk/arcgis/rest/services/Planning_Applications/MapServer/0/query",
    "Guildford": "https://www2.guildford.gov.uk/arcgis/rest/services/Planning/PlanningApplications/MapServer/0/query",
    "Surrey Heath": "https://maps.surreyheath.gov.uk/arcgis/rest/services/Planning/PlanningApplications/MapServer/0/query"
}

TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "conservation", "birch", "oak", "ash", "sycamore", "willow"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "conversion"]

def get_d(r):
    v = r.get("DATEAPVAL") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("VALIDATED") or 0
    return float(v)

def classify(r):
    p = str(r.get("PROPOSAL") or r.get("DESCRIPTION") or r.get("DESCRIPT") or "").lower()
    if not p: return False, 0
    m = [k for k in TREE_WORDS if k in p]
    s = len(m)
    if "tree" in p: s += 2
    if any(x in p for x in ["fell", "remove", "crown", "reduce", "thin"]): s += 5
    if any(w in p for w in SKIP_WORDS) and s < 7: return False, 0
    return (s > 1), s

def fetch_council(url):
    h = {"User-Agent": "Mozilla/5.0 Chrome/121.0.0.0"}
    q = {"where": "1=1", "outFields": "*", "returnGeometry": "false", "resultRecordCount": 150, "orderByFields": "OBJECTID DESC", "f": "json"}
    try:
        res = requests.get(url, params=q, headers=h, timeout=15)
        return [f.get("attributes", {}) for f in res.json().get("features", [])]
    except: return []

# --- DATABASE ---

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
        conn.commit(); conn.close()
    except: pass

# --- EMAILS ---

def send_email(to, subject, html):
    payload = {"from": "Vector Data Labs <onboarding@resend.dev>", "to": [to], "subject": subject, "html": html}
    requests.post(R_URL, json=payload, headers={"Authorization": f"Bearer {R_KEY}", "Content-Type": "application/json"})

def get_html(cn_name, ld, url, c_name):
    clean = ld['scope_summary'].replace('\r', '<br/>').replace('\n', '<br/>')
    return f"""<div style="font-family:Arial;max-width:600px;margin:auto;border:1px solid #ddd;border-radius:10px;overflow:hidden;">
    <div style="background:#2e7d32;padding:20px;text-align:center;color:white;"><h2 style="margin:0;">New Tree Lead: {c_name}</h2></div>
    <div style="padding:30px;"><p>Hello {cn_name}, a new job is available in the {c_name} area:</p>
    <div style="background:#f9f9f9;padding:15px;border-radius:5px;border:1px solid #eee;">
    <p><strong>Work:</strong><br/>{clean}</p><p><strong>Postcode:</strong> {ld['postcode']}</p></div>
    <div style="text-align:center;margin:30px 0;"><a href="{url}" style="background:#2e7d32;color:white;padding:15px 30px;text-decoration:none;border-radius:5px;font-weight:bold;font-size:18px;">Secure Lead</a></div>
    </div></div>"""

# --- ROUTES ---

@app.get("/test-regional", tags=["Diagnostics"])
def test_all():
    """Scoreboard to see how many leads are in each council area."""
    results = {}
    for name, url in COUNCILS.items():
        recs = fetch_council(url)
        found = [r for r in recs if classify(r)[0]]
        results[name] = {"scanned": len(recs), "leads": len(found)}
    return results

@app.get("/trigger-scrape", tags=["Live"])
def scrape(secret: str = Query(..., description="TRIGGER_SECRET")):
    if secret != T_SEC: raise HTTPException(status_code=401)
    leads_sent = 0
    for c_name, c_url in COUNCILS.items():
        recs = fetch_council(c_url)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).timestamp() * 1000
        for r in recs:
            ref = r.get("REFVAL") or r.get("REFERENCE") or r.get("PLANNO") or str(r.get("OBJECTID"))
            is_t, s = classify(r)
            if is_t and get_d(r) >= cutoff and not is_already_sent(ref):
                # AI
                prop = r.get('PROPOSAL') or r.get('DESCRIPTION') or r.get('DESCRIPT')
                addr = r.get('ADDRESS') or r.get('LOCATION') or r.get('SITE_ADDRESS')
                ai = client.chat.completions.create(
                    model="gpt-4o-mini", response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)"},
                              {"role": "user", "content": f"Addr: {addr} Prop: {prop}"}]
                )
                ld = json.loads(ai.choices[0].message.content)
                # Contractors
                cons = []
                if SURL:
                    try:
                        db = psycopg2.connect(SURL); c = db.cursor()
                        c.execute("SELECT id, business_name, email FROM tree_surgeons WHERE active IS TRUE")
                        for row in c.fetchall(): cons.append({"id": row[0], "name": row[1], "email": row[2]})
                        db.close()
                    except: pass
                if not cons: cons.append({"id": 1, "name": "Test User", "email": T_EM})
                for cn in cons:
                    amt = 4500 if ld.get("high_value") else 2500
                    sess = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Exclusive Lead: {c_name}"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(cn["id"]), "ref": ref, "site_address": ld.get("site_address"), "applicant_name": ld.get("applicant_name")}
                    )
                    send_email(cn["email"], f"New Tree Lead: {c_name} Area", get_html(cn["name"], ld, sess.url, c_name))
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
                html = f"<h2>Lead Unlocked</h2><p>Address: {m.get('site_address')}</p><p>Applicant: {m.get('applicant_name')}</p>"
                send_email(T_EM, "Lead Paid Successfully", html)
    except: pass
    return {"status": "ok"}

@app.get("/payment-success", include_in_schema=False)
def success(): return {"message": "Payment Successful"}

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return {"message": "Payment Cancelled"}
