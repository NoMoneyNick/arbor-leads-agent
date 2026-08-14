import os, json, logging, requests, psycopg2, stripe
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Header
from openai import OpenAI

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# ENVIRONMENT VARIABLES
OKEY = os.getenv("OPENAI_API_KEY")
SURL = os.getenv("SUPABASE_DB_URL")
S_SEC = os.getenv("STRIPE_SECRET_KEY")
S_WH = os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY = os.getenv("RESEND_API_KEY")
T_EM = os.getenv("TEST_EMAIL")
T_SEC = os.getenv("TRIGGER_SECRET")
P_URL = os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
# Going back to Layer 12 - the only one that we know sends data reliably
L_URL = "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query"

client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# Broadened keywords to catch more tree work
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "birch", "oak", "ash", "sycamore", "conservation"]
BAD_WORDS = ["dwelling", "new build", "erection of", "conversion"]

def get_date(r):
    # Try every possible date field Leeds uses
    val = r.get("DATEAPVAL") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or 0
    return float(val)

def classify(r):
    prop = str(r.get("PROPOSAL") or "").lower()
    if not prop: return False, 0
    matches = [k for k in TREE_WORDS if k in prop]
    score = len(matches)
    if "tree" in prop: score += 2
    if any(x in prop for x in ["fell", "remove", "crown"]): score += 5
    # If it's a house extension that happens to mention a tree, skip it unless it's a big tree job
    if any(b in prop for b in BAD_WORDS) and score < 7: return False, 0
    return (score > 2), score

def fetch_data():
    # Fetch 300 records. No sorting/filtering on server side to avoid 400 errors.
    params = {
        "where": "1=1",
        "outFields": "REFVAL,ADDRESS,PROPOSAL,DATEAPVAL,DATE_RECEIVED,OBJECTID",
        "resultRecordCount": 300,
        "f": "json"
    }
    try:
        res = requests.get(L_URL, params=params, timeout=20)
        data = res.json()
        recs = [f.get("attributes", {}) for f in data.get("features", [])]
        # Sort by Date then by ObjectID (Higher ID usually means newer)
        recs.sort(key=lambda x: (get_date(x), x.get("OBJECTID", 0)), reverse=True)
        return recs
    except Exception as e:
        logger.error(f"Fetch failed: {e}")
        return []

@app.get("/")
def home(): return {"status": "V2.2 - Data Flow Recovery"}

@app.get("/test-leeds")
def test():
    recs = fetch_data()
    # Looking for items from the last 120 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=120)).timestamp() * 1000
    leads = []
    for r in recs:
        is_tree, score = classify(r)
        d = get_date(r)
        if is_tree and d >= cutoff:
            r["_score"] = score
            r["_date"] = datetime.fromtimestamp(d/1000, tz=timezone.utc).strftime("%Y-%m-%d")
            leads.append(r)
    return {
        "total_scanned_from_council": len(recs),
        "tree_leads_found": len(leads),
        "leads": leads
    }

@app.get("/trigger-scrape")
def scrape(x_trigger_secret: str = Header(default=None)):
    if x_trigger_secret != T_SEC: raise HTTPException(status_code=401)
    recs = fetch_data()
    # Trigger only looks at the last 30 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    found = []
    for r in recs:
        is_tree, score = classify(r)
        if is_tree and get_date(r) >= cutoff:
            r["_score"] = score
            found.append(r)
    if not found: return {"status": "no new leads"}
    found.sort(key=lambda x: x["_score"], reverse=True)
    best = found[0]
    
    # AI Process
    ai_msg = f"Ref: {best.get('REFVAL')} Addr: {best.get('ADDRESS')} Prop: {best.get('PROPOSAL')}"
    ai_res = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)"},
            {"role": "user", "content": ai_msg}
        ]
    )
    ld = json.loads(ai_res.choices[0].message.content)
    
    # Contractors
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
            line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Tree Lead: {ld.get('postcode')}"}, "unit_amount": amt}, "quantity": 1}],
            mode="payment",
            success_url=f"{P_URL}/payment-success",
            cancel_url=f"{P_URL}/payment-cancelled",
            metadata={"surgeon_id": str(cn["id"]), "postcode": ld.get("postcode", ""), "site_address": ld.get("site_address", ""), "ref": best.get("REFVAL", "")}
        )
        requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [cn["email"]], "subject": "New Lead", "text": f"New Job: {ld['scope_summary']}. Link: {sess.url}"}, headers={"Authorization": f"Bearer {R_KEY}", "Content-Type": "application/json"})

    return {"status": "sent", "address": ld["site_address"]}

@app.post("/webhook")
async def webhook(req: Request):
    sig = req.headers.get("stripe-signature")
    payload = await req.body()
    try:
        event = stripe.Webhook.construct_event(payload, sig, S_WH)
        if event["type"] == "checkout.session.completed":
            sess = event["data"]["object"]
            if sess["id"] not in _processed:
                _processed.add(sess["id"])
                m = sess["metadata"]
                requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [T_EM], "subject": "Lead Paid!", "text": f"PAID: {m.get('ref')} - {m.get('site_address')}"}, headers={"Authorization": f"Bearer {R_KEY}", "Content-Type": "application/json"})
    except: pass
    return {"status": "ok"}

@app.get("/payment-success")
def success(): return {"message": "Success"}

@app.get("/payment-cancelled")
def cancel(): return {"message": "Cancelled"}
