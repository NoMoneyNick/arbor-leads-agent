import os
import json
import logging
from datetime import datetime, timezone, timedelta

import requests
import psycopg2
from fastapi import FastAPI, Request, HTTPException, Header
from openai import OpenAI
import stripe


# ============================================================
# VECTOR DATA LABS - LEEDS PRODUCTION VERSION 1.6
# ============================================================

app = FastAPI()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")

TEST_EMAIL = os.getenv("TEST_EMAIL")
TRIGGER_SECRET = os.getenv("TRIGGER_SECRET")
PUBLIC_APP_URL = os.getenv("PUBLIC_APP_URL")

RESEND_URL = "https://api.resend.com/emails"

# Layer 12 is the most complete history of Leeds Planning
LEEDS_PLANNING_URL = (
    "https://mapservices.leeds.gov.uk/"
    "arcgis/rest/services/Public/Planning/MapServer/12/query"
)

# ============================================================
# CLIENTS
# ============================================================

openai_client = OpenAI(api_key=OPENAI_API_KEY)
stripe.api_key = STRIPE_SECRET_KEY
_processed_sessions_memory = set()

# ============================================================
# TREE KEYWORDS & LOGIC
# ============================================================

TREE_KEYWORDS = [
    "tree", "trees", "tpo", "felling", "fell", "crown", "pruning", 
    "arboricultural", "stump", "birch", "oak", "sycamore", "ash", "cedar", 
    "conifer", "pollard", "reduction", "thinning", "conservation area"
]

SKIP_WORDS = ["dwelling", "extension", "new build", "erection of"]

# ============================================================
# HELPERS
# ============================================================

def safe_string(value):
    return str(value).strip() if value else ""

def get_best_date(record):
    """Tries multiple date fields used by Leeds Council."""
    # Try DATEAPVAL first, then DATE_RECEIVED as a backup
    val = record.get("DATEAPVAL") or record.get("DATE_RECEIVED")
    try:
        return float(val) if val else 0
    except:
        return 0

def classify_tree_application(record):
    proposal = safe_string(record.get("PROPOSAL")).lower()
    if not proposal: return {"is_tree_related": False}

    matches = [k for k in TREE_KEYWORDS if k in proposal]
    score = len(matches)
    
    if any(x in proposal for x in ["fell", "remove", "crown", "tpo"]):
        score += 5
        
    is_construction = any(w in proposal for w in SKIP_WORDS)

    if len(matches) > 0 and not is_construction:
        return {"is_tree_related": True, "score": score}
    if score > 8: 
        return {"is_tree_related": True, "score": score}
        
    return {"is_tree_related": False}

# ============================================================
# DATA FETCHING (BASIC & ROBUST)
# ============================================================

def fetch_leeds_records(max_records=1000):
    """Fetches records without complex server-side sorting to prevent 400 errors."""
    
    # We ask for all fields but NO sorting. Sorting on the council side breaks the query.
    params = {
        "where": "1=1",
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": max_records,
        "f": "json",
    }
    
    logger.info(f"Attempting to fetch {max_records} raw records from Leeds...")
    
    try:
        response = requests.get(LEEDS_PLANNING_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if "error" in data:
            logger.error(f"Leeds API returned error: {data['error']}")
            return []
            
        features = data.get("features", [])
        records = [f.get("attributes", {}) for f in features]
        
        # WE sort them here in Python instead of asking the Council to do it
        records.sort(key=lambda x: get_best_date(x), reverse=True)
        
        logger.info(f"Successfully retrieved and sorted {len(records)} records.")
        return records

    except Exception as e:
        logger.error(f"Critical Fetch Error: {e}")
        return []

# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def health_check():
    return {"status": "Leeds Tree Agent V1.6 Active"}

@app.get("/test-leeds")
def test_leeds():
    raw_records = fetch_leeds_records(max_records=1000)
    
    # Filter for last 120 days
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=120)).timestamp() * 1000
    
    valid_leads = []
    debug_dates = []
    
    for r in raw_records:
        date_val = get_best_date(r)
        
        # Collect the first few dates we see for debugging
        if date_val > 0 and len(debug_dates) < 5:
            readable = datetime.fromtimestamp(date_val/1000, tz=timezone.utc).strftime("%Y-%m-%d")
            debug_dates.append(readable)
        
        if date_val >= cutoff_ms:
            classification = classify_tree_application(r)
            if classification["is_tree_related"]:
                r["_score"] = classification["score"]
                r["_date"] = datetime.fromtimestamp(date_val/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                valid_leads.append(r)

    return {
        "total_downloaded": len(raw_records),
        "newest_dates_found": debug_dates,
        "tree_leads_found": len(valid_leads),
        "leads": valid_leads[:20]
    }

@app.get("/trigger-scrape")
def trigger_scrape(x_trigger_secret: str = Header(default=None)):
    if not TRIGGER_SECRET or x_trigger_secret != TRIGGER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw_records = fetch_leeds_records(max_records=500)
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    
    tree_apps = []
    for r in raw_records:
        date_val = get_best_date(r)
        if date_val >= cutoff_ms:
            cls = classify_tree_application(r)
            if cls["is_tree_related"]:
                r["_score"] = cls["score"]
                tree_apps.append(r)

    if not tree_apps:
        return {"status": "No leads found recently."}

    tree_apps.sort(key=lambda x: x["_score"], reverse=True)
    best = tree_apps[0]
    
    lead_data = extract_lead_with_openai(best)
    contractors = get_test_contractors()
    
    for c in contractors:
        try:
            session = create_test_checkout(c, lead_data, best.get("REFVAL"))
            send_tree_lead_email(c, lead_data, best.get("REFVAL"), session)
        except: pass

    return {"status": "Lead Sent", "address": lead_data["site_address"]}

# ============================================================
# STABLE UTILITIES
# ============================================================

def extract_lead_with_openai(record):
    raw_text = f"Ref: {record.get('REFVAL')}\nAddr: {record.get('ADDRESS')}\nProp: {record.get('PROPOSAL')}"
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)."},
            {"role": "user", "content": raw_text}
        ]
    )
    return json.loads(response.choices[0].message.content)

def create_test_checkout(contractor, lead, ref):
    amount = 4500 if lead.get("high_value") else 2500
    return stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Tree Lead: {lead.get('postcode', ref)}"}, "unit_amount": amount}, "quantity": 1}],
        mode="payment",
        success_url=f"{PUBLIC_APP_URL}/payment-success",
        cancel_url=f"{PUBLIC_APP_URL}/payment-cancelled",
        metadata={
            "surgeon_id": str(contractor["id"]),
            "postcode": lead.get("postcode", ""),
            "site_address": lead.get("site_address", ""),
            "application_reference": ref
        }
    )

def send_tree_lead_email(contractor, lead, ref, session):
    body = f"Hi {contractor['name']},\n\nNew Tree Job in {lead.get('postcode')}:\n{lead['scope_summary']}\n\nLink: {session.url}"
    payload = {"from": "Vector Data Labs <onboarding@resend.dev>", "to": [contractor["email"]], "subject": f"New Lead: {ref}", "text": body}
    requests.post(RESEND_URL, json=payload, headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"})

def get_test_contractors():
    contractors = []
    if SUPABASE_DB_URL:
        try:
            conn = psycopg2.connect(SUPABASE_DB_URL)
            with conn.cursor() as cur:
                cur.execute("SELECT id, business_name, email FROM tree_surgeons WHERE active IS TRUE;")
                for r in cur.fetchall():
                    contractors.append({"id": r[0], "name": r[1], "email": r[2]})
            conn.close()
        except: pass
    if not contractors:
        contractors.append({"id": 1, "name": "Test User", "email": TEST_EMAIL})
    return contractors

@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            if session["id"] not in _processed_sessions_memory:
                _processed_sessions_memory.add(session["id"])
                m = session["metadata"]
                msg = f"UNLOCKED:\nRef: {m['application_reference']}\nAddr: {m['site_address']}"
                requests.post(RESEND_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [TEST_EMAIL], "subject": "Lead Paid!", "text": msg}, headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"})
    except: pass
    return {"status": "success"}

@app.get("/payment-success")
def payment_success(): return {"message": "Success"}

@app.get("/payment-cancelled")
def payment_cancelled(): return {"message": "Cancelled"}
