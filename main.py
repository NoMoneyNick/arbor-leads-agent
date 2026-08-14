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
# VECTOR DATA LABS - LEEDS PRODUCTION VERSION
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

# We use Layer 12 (Planning Applications) but also watch Layer 11 (Validations)
LEEDS_PLANNING_URL = (
    "https://mapservices.leeds.gov.uk/"
    "arcgis/rest/services/Public/Planning/MapServer/12/query"
)

REQUIRED_ENV_VARS = {
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
    "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
    "RESEND_API_KEY": RESEND_API_KEY,
    "TEST_EMAIL": TEST_EMAIL,
    "TRIGGER_SECRET": TRIGGER_SECRET,
}

# ============================================================
# CLIENTS
# ============================================================

openai_client = OpenAI(api_key=OPENAI_API_KEY)
stripe.api_key = STRIPE_SECRET_KEY

# Fallback memory for webhooks
_processed_sessions_memory = set()

# ============================================================
# TREE KEYWORDS & LOGIC
# ============================================================

STRONG_TREE_KEYWORDS = [
    "tree", "trees", "tpo", "tree preservation order", "arboricultural", 
    "arboriculture", "arborist", "tree surgeon", "tree surgery", "tree works", 
    "tree work", "tree removal", "tree felling", "felling", "fell tree", 
    "fell trees", "crown reduction", "crown lifting", "crown thinning", 
    "pollard", "pruning", "stump grinding", "dead tree", "dangerous tree"
]

NON_TREE_WORDS = [
    "erection of", "dwelling", "extension", "change of use", "new build",
    "demolition of", "convert", "conversion", "housing development"
]

# ============================================================
# HELPERS
# ============================================================

def safe_string(value):
    return str(value).strip() if value else ""

def milliseconds_to_date(value):
    if not value: return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except: return None

def classify_tree_application(record):
    """Determines if a record is actually about trees."""
    proposal = safe_string(record.get("PROPOSAL")).lower()
    
    if not proposal:
        return {"is_tree_related": False, "score": 0, "matched": []}

    matched = [k for k in STRONG_TREE_KEYWORDS if k in proposal]
    negatives = [n for n in NON_TREE_WORDS if n in proposal]

    score = len(matched) * 2
    
    # Bonus for critical terms
    if "fell" in proposal or "felling" in proposal: score += 5
    if "tpo" in proposal or "preservation order" in proposal: score += 5
    if "dangerous" in proposal or "dead" in proposal: score += 3

    # Penalty for general construction projects
    if negatives and len(matched) < 3:
        score -= 10

    return {
        "is_tree_related": score >= 5,
        "score": score,
        "matched": matched,
        "reason": "High tree keyword density" if score >= 5 else "Insufficient tree focus"
    }

# ============================================================
# DATA FETCHING (THE FIX FOR OLD DATA)
# ============================================================

def fetch_leeds_records(max_records=100):
    """
    Fetches the NEWEST planning applications from Leeds.
    We filter by date at the database level so we don't get 1990s data.
    """
    # Calculate date 30 days ago for the API filter
    thirty_days_ago = datetime.now() - timedelta(days=30)
    formatted_date = thirty_days_ago.strftime("%Y-%m-%d")

    # SQL-like filter for the ArcGIS API
    # We look for records received after our 'thirty_days_ago' date
    where_clause = f"DATE_RECEIVED >= DATE '{formatted_date}'"

    params = {
        "where": where_clause,
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": max_records,
        "orderByFields": "DATE_RECEIVED DESC", # Get newest first
        "f": "json",
    }

    logger.info(f"Requesting Leeds data newer than {formatted_date}")
    
    try:
        response = requests.get(LEEDS_PLANNING_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            logger.error(f"Leeds API Error: {data['error']}")
            return []

        features = data.get("features", [])
        return [f.get("attributes", {}) for f in features]

    except Exception as e:
        logger.error(f"Fetch failed: {e}")
        return []

# ============================================================
# CORE ROUTES
# ============================================================

@app.get("/")
def health_check():
    return {"status": "Leeds Tree Agent Active", "time": datetime.now()}

@app.get("/test-leeds")
def test_leeds():
    """Diagnostic route to see what Leeds is currently providing."""
    records = fetch_leeds_records(max_records=50)
    
    valid_leads = []
    for r in records:
        classification = classify_tree_application(r)
        if classification["is_tree_related"]:
            # Enrich the record for the test view
            r["_ai_score"] = classification["score"]
            r["_matched_keywords"] = classification["matched"]
            r["_readable_date"] = milliseconds_to_date(r.get("DATE_RECEIVED"))
            valid_leads.append(r)

    return {
        "total_records_checked": len(records),
        "tree_leads_found": len(valid_leads),
        "leads": valid_leads
    }

@app.get("/trigger-scrape")
def trigger_scrape(x_trigger_secret: str = Header(default=None)):
    if not TRIGGER_SECRET or x_trigger_secret != TRIGGER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    records = fetch_leeds_records(max_records=100)
    
    # Find all tree jobs
    tree_apps = []
    for r in records:
        cls = classify_tree_application(r)
        if cls["is_tree_related"]:
            r["_score"] = cls["score"]
            tree_apps.append(r)

    if not tree_apps:
        return {"status": "No new tree leads found in the last 30 days."}

    # Sort by highest score (best jobs first)
    tree_apps.sort(key=lambda x: x["_score"], reverse=True)
    selected_record = tree_apps[0]
    
    # Extract details via AI
    lead = extract_lead_with_openai(selected_record)
    
    # Get contractors and send emails (Same logic as your working version)
    contractors = get_test_contractors()
    results = []

    for c in contractors:
        try:
            session = create_test_checkout(c, lead, selected_record.get("REFVAL"))
            send_tree_lead_email(c, lead, selected_record.get("REFVAL"), session, selected_record)
            results.append({"contractor": c["name"], "status": "sent"})
        except Exception as e:
            results.append({"contractor": c["name"], "status": "failed", "error": str(e)})

    return {"status": "Pipeline completed", "lead_found": lead["site_address"], "results": results}

# ============================================================
# AI & STRIPE HELPERS (STABLE)
# ============================================================

def extract_lead_with_openai(record):
    raw_text = f"Ref: {record.get('REFVAL')}\nApp: {record.get('APPNAME')}\nAddr: {record.get('ADDRESS')}\nProp: {record.get('PROPOSAL')}"
    
    system_prompt = "You are a UK planning extractor. Return ONLY JSON with: applicant_name, site_address, postcode, scope_summary, high_value (true/false)."
    
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_text}
        ]
    )
    return json.loads(response.choices[0].message.content)

def create_test_checkout(contractor, lead, ref):
    amount = 4500 if lead.get("high_value") else 2500
    return stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Lead: {lead['postcode']}"}, "unit_amount": amount}, "quantity": 1}],
        mode="payment",
        success_url=f"{PUBLIC_APP_URL}/payment-success",
        cancel_url=f"{PUBLIC_APP_URL}/payment-cancelled",
        metadata={
            "surgeon_id": str(contractor["id"]),
            "postcode": lead["postcode"],
            "site_address": lead["site_address"],
            "applicant_name": lead["applicant_name"],
            "scope_summary": lead["scope_summary"],
            "application_reference": ref
        }
    )

def send_test_email(recipient, subject, body, sender_name="Vector Data Labs"):
    payload = {"from": f"{sender_name} <onboarding@resend.dev>", "to": [recipient], "subject": subject, "text": body}
    headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
    requests.post(RESEND_URL, json=payload, headers=headers, timeout=10)

def send_tree_lead_email(contractor, lead, ref, session, record):
    body = f"Hello {contractor['name']},\n\nNew Tree Lead Found:\nAddress: {lead['site_address']}\nPostcode: {lead['postcode']}\nWork: {lead['scope_summary']}\n\nLink: {session.url}"
    send_test_email(contractor["email"], f"New Lead: {lead['postcode']}", body)

def get_test_contractors():
    # Attempt DB fetch, fallback to test email
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
        contractors.append({"id": 999, "name": "Test User", "email": TEST_EMAIL})
    return contractors

# ============================================================
# STRIPE WEBHOOK (STABLE)
# ============================================================

@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except: raise HTTPException(status_code=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if session["id"] in _processed_sessions_memory: return {"status": "ignored"}
        _processed_sessions_memory.add(session["id"])
        
        m = session["metadata"]
        unlock_msg = f"LEAD UNLOCKED\n\nRef: {m['application_reference']}\nName: {m['applicant_name']}\nAddr: {m['site_address']}\nScope: {m['scope_summary']}"
        send_test_email(TEST_EMAIL, "Lead Unlocked!", unlock_msg)

    return {"status": "success"}

@app.get("/payment-success")
def payment_success(): return {"message": "Test Payment Successful"}

@app.get("/payment-cancelled")
def payment_cancelled(): return {"message": "Test Payment Cancelled"}
