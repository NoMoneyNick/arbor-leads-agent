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

# Expanded tree keywords
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "conservation", "birch", "oak", "ash", "sycamore", "willow"]
SKIP_WORDS = ["dwelling", "erection of", "new build"]

def get_d(r):
    # Try all known Leeds date fields
    v = r.get("DATEAPVAL") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("DATEDECISS") or 0
    return float(v)

def classify(r):
    p = str(r.get("PROPOSAL") or r.get("DESCRIPT") or "").lower()
    if not p: return False, 0
    m = [k for k in TREE_WORDS if k in p]
    s = len(m)
    if "tree" in p: s += 2
    if any(x in p for x in ["fell", "remove", "crown", "reduce", "thin"]): s += 5
    is_bad = any(w in p for w in SKIP_WORDS)
    if is_bad and s < 7: return False, 0
    return (s > 0), s

def fetch():
    h = {"User-Agent": "Mozilla/5.0 Chrome/121.0.0.0", "Referer": "https://www.leeds.gov.uk/"}
    # We sort by OBJECTID DESC to get the newest entries first
    q = {
        "where": "1=1",
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": 1000,
        "orderByFields": "OBJECTID DESC",
        "f": "json"
    }
    try:
        res = requests.get(L_URL, params=q, headers=h, timeout=30)
        data = res.json()
        return [f.get("attributes", {}) for f in data.get("features", [])]
    except: return []

@app.get("/")
def home(): return {"status": "V2.7 ID-Sort Active"}

@app.get("/test-leeds")
def test():
    recs = fetch()
    # Looking for records in the last 6 months (Leeds updates can be slow)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).timestamp() * 1000
    leads = []
    samples = []
    
    for i, r in enumerate(recs):
        d = get_d(r)
        if i < 5:
            samples.append({"id": r.get("OBJECTID"), "date": d, "text": r.get("PROPOSAL")})
            
        is_t, s = classify(r)
        # If date is 0 but ID is very high, it's likely a brand new record not yet dated
        if is_t and (d >= cutoff or d == 0):
            r["_score"] = s
            r["_date"] = datetime.fromtimestamp(d/1000, tz=timezone.utc).strftime("%Y-%m-%d") if d > 0 else "Pending"
            leads.append(r)
            
    return {
        "council_count": len(recs),
        "debug_samples": samples,
        "leads_found": len(leads),
        "leads": leads[:30]
    }

@app.get("/trigger-scrape")
def scrape(x_trigger_secret: str = Header(default=None)):
    if x_trigger_secret != T_SEC: raise HTTPException(status_code=401)
    recs = fetch()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    found = []
    for r in recs:
        d = get_d(r)
        is_t, s = classify(r)
        if is_t and (d >= cutoff or d == 0):
            r["_score"] = s
            found.append(r)
    if not found: return {"status": "no leads"}
    found.sort(key=lambda x: x["_score"], reverse=True)
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
