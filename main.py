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
# VECTOR DATA LABS - LEEDS PRODUCTION VERSION 1.1
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

# We use Layer 12 (Planning Applications)
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

_processed_sessions_memory = set()

# ============================================================
# TREE KEYWORDS & LOGIC
# ============================================================

STRONG_TREE_KEYWORDS = [
    "tree", "trees", "tpo", "tree preservation order", "arboricultural", 
    "arboriculture", "arborist", "tree surgeon", "tree surgery", "tree works", 
    "tree work", "tree removal", "tree felling", "felling", "fell tree", 
    "fell trees", "crown reduction", "crown lifting", "crown thinning", 
    "pollard", "pruning", "stump grinding", "dead tree", "dangerous tree",
    "sycamore", "oak", "ash dieback", "conifer", "beech", "birch"
]

# We are more careful here. We only penalize if it's a huge build.
CONSTRUCTION_WORDS = [
    "451 dwellings", "900 dwellings", "residential development", "office building"
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
    
    # Calculate score
    score = len(matched) * 2
    if "fell" in proposal or "felling" in proposal or "remove" in proposal: score += 5
    if "tpo" in proposal or "preservation order" in proposal: score += 5
    
    # Check for big construction
    is_big_construction = any(w in proposal for w in CONSTRUCTION_WORDS)
    
    # It's a tree job if it has keywords AND isn't just a big housing estate (unless it specifically mentions felling)
    is_tree_related = score >= 4
    if is_big_construction and score < 10:
        is_tree_related = False

    return {
        "is_tree_related": is_tree_related,
        "score": score,
        "matched": matched
    }

# ============================================================
# DATA FETCHING
# ============================================================

def fetch_leeds_records(max_records=100):
    """
    Fetches planning applications using DATEAPVAL (Date Application Validated).
    """
    # Calculate timestamp for 60 days ago (Leeds can be slow to update)
    cutoff_date = datetime.now() - timedelta(days=60)
    cutoff_timestamp = int(cutoff_date.timestamp() * 1000)

    # Simplified where clause using a field we know exists (DATEAPVAL)
    where_clause = f"DATEAPVAL >= {cutoff_timestamp}"

    params = {
        "where": where_clause,
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": max_records,
        "orderByFields": "DATEAPVAL DESC",
        "f": "json",
    }

    logger.info(f"Requesting Leeds records since timestamp: {cutoff_timestamp}")
    
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
# ROUTES
# ============================================================

@app.get("/")
def health_check():
    return {"status": "Leeds Tree Agent Version 1.1 Active"}

@app.get("/test-leeds")
def test_leeds():
    records = fetch_leeds_records(max_records=100)
    
    valid_leads = []
    for r in records:
        classification = classify_tree_application(r)
        if classification["is_tree_related"]:
            r["_ai_score"] = classification["score"]
            r["_matched_keywords"] = classification["matched"]
            r["_readable_date"] = milliseconds_to_date(r.get("DATEAPVAL"))
            valid_leads.append(r)

    return {
        "total_records_returned_by_council": len(records),
        "tree_leads_found": len(valid_leads),
        "leads": valid_leads
    }

@app.get("/trigger-scrape")
def trigger_scrape(x_trigger_secret: str = Header(default=None)):
    if not TRIGGER_SECRET or x_trigger_secret != TRIGGER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    records = fetch_leeds_records(max_records=50)
    
    tree_apps = []
    for r in records:
        cls = classify_tree_application(r)
        if cls["is_tree_related"]:
            r["_score"] = cls["score"]
            tree_apps.append(r)

    if not tree_apps:
        return {"status": "No tree leads found in recent records."}

    # Process the best lead
    tree_apps.sort(key=lambda x: x["_score"], reverse=True)
    selected_record = tree_apps[0]
    lead = extract_lead_with_openai(selected_record)
    
    contractors = get_test_contractors()
    results = []

    for c in contractors:
        try:
            session = create_test_checkout(c, lead, selected_record.get("REFVAL"))
            send_tree_lead_email(c, lead, selected_record.get("REFVAL"), session, selected_record)
            results.append({"contractor": c["name"], "status": "sent"})
        except Exception as e:
            results.append({"contractor": c["name"], "status": "failed", "error": str(e)})

    return {"status": "Completed", "lead_found": lead["site_address"], "results": results}

# ============================================================
# AI & STRIPE & EMAIL (STABLE)
# ============================================================

def extract_lead_with_openai(record):
    raw_text = f"Ref: {record.get('REFVAL')}\nAddr: {record.get('ADDRESS')}\nProp: {record.get('PROPOSAL')}"
    system_prompt = "Return ONLY JSON with: applicant_name, site_address, postcode, scope_summary, high_value (bool)."
    
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": raw_text}]
    )
    return json.loads(response.choices[0].message.content)

def create_test_checkout(contractor, lead, ref):
    amount = 4500 if lead.get("high_value") else 2500
    return stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Lead: {lead.get('postcode', ref)}"}, "unit_amount": amount}, "quantity": 1}],
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

def send_tree_lead_email(contractor, lead, ref, session, record):
    body = f"Hello {contractor['name']},\n\nNew Lead Found:\n{lead['site_address']}\nWork: {lead['scope_summary']}\n\nLink: {session.url}"
    payload = {"from": "Vector Data Labs <onboarding@resend.dev>", "to": [contractor["email"]], "subject": f"New Lead: {ref}", "text": body}
    headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
    requests.post(RESEND_URL, json=payload, headers=headers)

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
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except: raise HTTPException(status_code=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if session["id"] in _processed_sessions_memory: return {"status": "ignored"}
        _processed_sessions_memory.add(session["id"])
        
        m = session["metadata"]
        msg = f"UNLOCKED: {m['application_reference']}\nAddress: {m['site_address']}"
        payload = {"from": "Vector Data Labs <onboarding@resend.dev>", "to": [TEST_EMAIL], "subject": "Lead Paid!", "text": msg}
        headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
        requests.post(RESEND_URL, json=payload, headers=headers)

    return {"status": "success"}

@app.get("/payment-success")
def payment_success(): return {"message": "Success"}

@app.get("/payment-cancelled")
def payment_cancelled(): return {"message": "Cancelled"}
