import os, json, logging, requests, psycopg2, stripe
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Header
from openai import OpenAI

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# ENV VARS
OKEY = os.getenv("OPENAI_API_KEY")
SURL = os.getenv("SUPABASE_DB_URL")
S_SEC = os.getenv("STRIPE_SECRET_KEY")
S_WH = os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY = os.getenv("RESEND_API_KEY")
T_EM = os.getenv("TEST_EMAIL")
T_SEC = os.getenv("TRIGGER_SECRET")
P_URL = os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
L_URL = "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query"

client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# LOGIC
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "birch", "oak", "sycamore", "ash", "conservation area"]
SKIP_WORDS = ["dwelling", "extension", "new build", "erection of"]

def get_date(r):
    val = r.get("DATEAPVAL") or r.get("DATE_RECEIVED") or r.get("DATEDECISS")
    return float(val) if val else 0

def classify(r):
    p = str(r.get("PROPOSAL") or "").lower()
    if not p: return False, 0
    matches = [k for k in TREE_WORDS if k in p]
    score = len(matches)
    if "tree" in p and "conservation area" in p: score += 10
    if any(x in p for x in ["fell", "remove", "crown", "tpo"]): score += 5
    is_bad = any(w in p for w in SKIP_WORDS)
    if matches and not is_bad: return True, score
    if score > 8: return True, score
    return False, 0

# FETCHING
def fetch_batch(offset):
    params = {
        "where": "1=1",
        "outFields": "REFVAL,ADDRESS,PROPOSAL,DATEAPVAL,DATE_RECEIVED",
        "resultRecordCount": 150,
        "resultOffset": offset,
        "f": "json"
    }
    try:
        res = requests.get(L_URL, params=params, timeout=15)
        data = res.json()
        return [f.get("attributes", {}) for f in data.get("features", [])]
    except: return []

def get_all_leeds():
    recs = []
    for i in range(3):
        batch = fetch_batch(i * 150)
        if not batch: break
        recs.extend(batch)
    recs.sort(key=lambda x: get_date(x), reverse=True)
    return recs

# ROUTES
@app.get("/")
def home(): return {"status": "V2.0 Slim Active"}

@app.get("/test-leeds")
def test():
    recs = get_all_leeds()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=120)).timestamp() * 1000
    leads = []
    debug = []
    for r in recs:
        d = get_date(r)
        if d > 0 and len(debug) < 5:
            debug.append(datetime.fromtimestamp(d/1000, tz=timezone.utc).strftime("%Y-%m-%d"))
        if d >= cutoff:
            is_tree, score = classify(r)
            if is_tree:
                r["_score"] = score
                r["_date"] = datetime.fromtimestamp(d/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                leads.append(r)
    return {"scanned": len(recs), "dates": debug, "found": len(leads), "leads": leads}

@app.get("/trigger-scrape")
def scrape(x_trigger_secret: str = Header(default=None)):
    if x_trigger_secret != T_SEC: raise HTTPException(status_code=401)
    recs = get_all_leeds()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    found = []
    for r in recs:
        if get_date(r) >= cutoff:
            is_tree, score = classify(r)
            if is_tree:
                r["_score"] = score
                found.append(r)
    if not found: return {"status": "no leads"}
    found.sort(key=lambda x: x["_score"], reverse=True)
    best = found[0]
    
    # AI Extraction
    txt = f"Ref: {best.get('REFVAL')} Addr: {best.get('ADDRESS')} Prop: {best.get('PROPOSAL')}"
    ai = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)"},
            {"role": "user", "content": txt}
        ]
    )
    ld = json.loads(ai.choices[0].message.content)
    
    # Contractors & Stripe
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
            line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Lead: {ld.get('postcode')}"}, "unit_amount": amt}, "quantity": 1}],
            mode="payment",
            success_url=f"{P_URL}/payment-success",
            cancel_url=f"{P_URL}/payment-cancelled",
            metadata={"surgeon_id": str(cn["id"]), "postcode": ld.get("postcode", ""), "site_address": ld.get("site_address", ""), "ref": best.get("REFVAL", "")}
        )
        # Email
        body = f"Hi {cn['name']}, New Tree Job: {ld['scope_summary']}. Link: {sess.url}"
        requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [cn["email"]], "subject": "New Lead", "text": body}, headers={"Authorization": f"Bearer {R_KEY}"})

    return {"status": "sent", "address": ld["site_address"]}

@app.post("/webhook")
async def webhook(req: Request):
    sig = req.headers.get("stripe-signature")
    body = await req.body()
    try:
        event = stripe.Webhook.construct_event(body, sig, S_WH)
        if event["type"] == "checkout.session.completed":
            sess = event["data"]["object"]
            if sess["id"] not in _processed:
                _processed.add(sess["id"])
                m = sess["metadata"]
                msg = f"PAID: {m.get('ref')} - {m.get('site_address')}"
                requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [T_EM], "subject": "Lead Paid!", "text": msg}, headers={"Authorization": f"Bearer {R_KEY}"})
    except: pass
    return {"status": "ok"}

@app.get("/payment-success")
def success(): return {"message": "Success"}

@app.get("/payment-cancelled")
def cancel(): return {"message": "Cancelled"}
