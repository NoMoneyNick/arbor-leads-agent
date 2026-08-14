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
    v = r.get("DATEAPVAL") or r.get("DATE_RECEIVED") or 0
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
        data = res.json()
        return [f.get("attributes", {}) for f in data.get("features", [])]
    except: return []

# --- EMAILS ---

def send_email(to, subject, html):
    payload = {"from": "Vector Data Labs <onboarding@resend.dev>", "to": [to], "subject": subject, "html": html}
    requests.post(R_URL, json=payload, headers={"Authorization": f"Bearer {R_KEY}", "Content-Type": "application/json"})

def get_lead_alert_html(cn_name, ld, url):
    # Clean up any weird symbols in the proposal text
    clean_summary = ld['scope_summary'].replace('\r', '<br/>').replace('\n', '<br/>')
    return f"""
    <div style="font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; max-width: 600px; margin: auto; border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden;">
        <div style="background-color: #2e7d32; padding: 20px; text-align: center;">
            <h1 style="color: white; margin: 0; font-size: 22px;">New Tree Lead Found</h1>
        </div>
        <div style="padding: 30px; color: #333; line-height: 1.6;">
            <p>Hello {cn_name},</p>
            <p>We've identified a new planning application that requires specialist arboricultural work.</p>
            <div style="background-color: #f5f5f5; padding: 20px; border-radius: 5px; margin: 20px 0;">
                <p style="margin: 0;"><strong>Work Required:</strong><br/>{clean_summary}</p>
                <p style="margin: 15px 0 0 0;"><strong>Location:</strong> {ld['postcode']}</p>
            </div>
            <div style="text-align: center; margin-top: 30px;">
                <a href="{url}" style="background-color: #2e7d32; color: white; padding: 14px 30px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block;">Purchase Exclusive Lead</a>
                <p style="font-size: 12px; color: #888; margin-top: 15px;">Secure payment via Stripe. One buyer only.</p>
            </div>
        </div>
        <div style="background-color: #f9f9f9; padding: 15px; text-align: center; border-top: 1px solid #eee; font-size: 12px; color: #999;">
            Vector Data Labs - Real-time Leeds Council Planning Alerts
        </div>
    </div>
    """

def get_unlock_html(m):
    return f"""
    <div style="font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; max-width: 600px; margin: auto; border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden;">
        <div style="background-color: #1565c0; padding: 20px; text-align: center;">
            <h1 style="color: white; margin: 0; font-size: 22px;">Lead Details Unlocked</h1>
        </div>
        <div style="padding: 30px; color: #333; line-height: 1.6;">
            <p>Your purchase was successful. Here are the contact and site details:</p>
            <div style="border: 1px solid #1565c0; padding: 20px; border-radius: 5px; margin: 20px 0;">
                <p style="margin: 0;"><strong>Site Address:</strong><br/>{m.get('site_address')}</p>
                <p style="margin: 15px 0 0 0;"><strong>Applicant Name:</strong><br/>{m.get('applicant_name') or 'Visit Site'}</p>
                <p style="margin: 15px 0 0 0;"><strong>Council Reference:</strong> {m.get('ref')}</p>
            </div>
            <p>We recommend visiting the site or writing to the applicant immediately while the lead is fresh.</p>
        </div>
        <div style="background-color: #f9f9f9; padding: 15px; text-align: center; border-top: 1px solid #eee; font-size: 12px; color: #999;">
            Vector Data Labs Purchase Receipt
        </div>
    </div>
    """

# --- ROUTES ---

@app.get("/", include_in_schema=False)
def home(): return {"message": "Go to /docs"}

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
    return {"council_count": len(recs), "leads_found": len(leads), "leads": leads[:20]}

@app.get("/trigger-scrape", tags=["Live"])
def scrape(secret: str = Query(..., description="Your secret key")):
    if secret != T_SEC: raise HTTPException(status_code=401)
    recs = fetch()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    found = [r for r in recs if get_d(r) >= cutoff and classify(r)[0]]
    if not found: return {"status": "no leads"}
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
        send_email(cn["email"], f"New Tree Job Opportunity: {ld.get('postcode')}", html)
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
                send_email(T_EM, "Lead Unlocked - Site Details Inside", html)
    except: pass
    return {"status": "ok"}

@app.get("/payment-success", include_in_schema=False)
def success(): return {"message": "Success"}

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return {"message": "Cancelled"}
