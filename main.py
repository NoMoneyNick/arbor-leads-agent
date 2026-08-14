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
# We are using the main layer that worked at the very start
L_URL = "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query"

client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "conservation"]
SKIP_WORDS = ["dwelling", "extension", "new build", "erection of"]

def get_date(r):
    # Leeds date fields
    v = r.get("DATEAPVAL") or r.get("DATE_RECEIVED") or 0
    return float(v)

def classify(r):
    p = str(r.get("PROPOSAL") or "").lower()
    if not p: return False, 0
    matches = [k for k in TREE_WORDS if k in p]
    score = len(matches)
    if "tree" in p: score += 2
    if any(x in p for x in ["fell", "remove", "crown"]): score += 5
    if any(w in p for w in SKIP_WORDS) and score < 7: return False, 0
    return (score > 1), score

def fetch_leeds_raw():
    # This is the EXACT request structure that worked at the start
    params = {
        "where": "1=1",
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": 1000,
        "f": "json"
    }
    # This header makes the server think we are a standard Chrome browser
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Referer": "https://www.leeds.gov.uk/"
    }
    try:
        res = requests.get(L_URL, params=params, headers=headers, timeout=30)
        data = res.json()
        if "features" in data:
            recs = [f.get("attributes", {}) for f in data["features"]]
            # Sort them NEWEST to OLDEST here in our code
            recs.sort(key=lambda x: get_date(x), reverse=True)
            return recs
        else:
            logger.error(f"Council returned no features: {data}")
            return []
    except Exception as e:
        logger.error(f"Council connection failed: {e}")
        return []

@app.get("/")
def home(): return {"status": "V2.4 Human-Mask Active"}

@app.get("/test-leeds")
def test():
    recs = fetch_leeds_raw()
    # Find leads from the last 90 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).timestamp() * 1000
    leads = []
    for r in recs:
        is_tree, score = classify(r)
        d = get_date(r)
        if is_tree and d >= cutoff:
            r["_score"] = score
            r["_date"] = datetime.fromtimestamp(d/1000, tz=timezone.utc).strftime("%Y-%m-%d")
            leads.append(r)
    return {
        "scanned_from_council": len(recs),
        "leads_found_in_90_days": len(leads),
        "leads": leads[:20]
    }

@app.get("/trigger-scrape")
def scrape(x_trigger_secret: str = Header(default=None)):
    if x_trigger_secret != T_SEC: raise HTTPException(status_code=401)
    recs = fetch_leeds_raw()
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
