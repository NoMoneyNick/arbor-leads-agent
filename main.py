import os, json, logging, requests, psycopg2, stripe
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Header
from openai import OpenAI

app = FastAPI()
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
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "conservation"]
SKIP_WORDS = ["dwelling", "extension", "new build", "erection of"]

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

def fetch():
    h = {"User-Agent": "Mozilla/5.0 Chrome/121.0.0.0", "Referer": "https://www.leeds.gov.uk/"}
    q = {"where": "1=1", "outFields": "*", "returnGeometry": "false", "resultRecordCount": 1000, "f": "json"}
    try:
        res = requests.get(L_URL, params=q, headers=h, timeout=30)
        data = res.json()
        recs = [f.get("attributes", {}) for f in data.get("features", [])]
        recs.sort(key=lambda x: get_d(x), reverse=True)
        return recs
    except: return []

@app.get("/")
def home(): return {"status": "V2.5 Active"}

@app.get("/test-leeds")
def test():
    recs = fetch()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).timestamp() * 1000
    leads = []
    for r in recs:
        is_t, s = classify(r)
        if is_t and get_d(r) >= cutoff:
            r["_score"] = s
            r["_date"] = datetime.fromtimestamp(get_d(r)/1000, tz=timezone.utc).strftime("%Y-%m-%d")
            leads.append(r)
    return {"scanned": len(recs), "found": len(leads), "leads": leads[:20]}

@app.get("/trigger-scrape")
def scrape(x_trigger_secret: str = Header(default=None)):
    if x_trigger_secret != T_SEC: raise HTTPException(status_code=401)
    recs = fetch()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    found = [r for r in recs if get_d(r) >= cutoff and classify(r)[0]]
    if not found: return {"status": "no leads"}
    found.sort(key=lambda x: classify(x)[1], reverse=True)
    best = found[0]
    
    # AI
    ai = client.chat.completions.create(
        model="gpt-4o-mini", response_format={"type": "json_object"},
        messages=[{"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)"},
                  {"role": "user", "content": f"Addr: {best.get('ADDRESS')} Prop: {best.get('PROPOSAL')}"}]
    )
    ld = json.loads(ai.choices[0].message.content)
    
    # Contractors
    cons = []
    if SURL:
        try:
            db = psycopg2.connect(SURL)
            with db.cursor() as c:
                c.execute("SELECT id, email FROM tree_surgeons WHERE active IS TRUE")
                for row in c.fetchall(): cons.append({"id": row[0], "email": row[1]})
            db.close()
        except: pass
    if not cons: cons.append({"id": 1, "email": T_EM})
    
    for cn in cons:
        amt = 4500 if ld.get("high_value") else 2500
        sess = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": "Tree Lead"}, "unit_amount": amt}, "quantity": 1}],
            mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
            metadata={"surgeon_id": str(cn["id"]), "ref": best.get("REFVAL", ""), "site_address": ld.get("site_address", "")}
        )
        requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [cn["email"]], "subject": "New Lead", "text": f"Job: {ld['scope_summary']}. Link: {sess.url}"}, headers={"Authorization": f"Bearer {R_KEY}"})
    return {"status": "sent"}

@app.post("/webhook")
async def webhook(req: Request):
    sig, payload = req.headers.get("stripe-signature"), await req.body()
    try:
        event = stripe.Webhook.construct_event(payload, sig, S_WH)
        if event["type"] == "checkout.session.completed":
            sess = event["data"]["object"]
            if sess["id"] not in _processed:
                _processed.add(sess["id"])
                m = sess["metadata"]
                requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [T_EM], "subject": "Paid!", "text": f"PAID: {m.get('site_address')}"}, headers={"Authorization": f"Bearer {R_KEY}"})
    except: pass
    return {"status": "ok"}

@app.get("/payment-success")
def success(): return {"message": "Success"}

@app.get("/payment-cancelled")
def cancel(): return {"message": "Cancelled"}
