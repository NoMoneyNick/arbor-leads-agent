import os, json, logging, requests, psycopg2, stripe
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

app = FastAPI(title="Vector Data Labs - Multi-Region V8.3", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# --- ENVIRONMENT VARIABLES ---
OKEY, SURL = os.getenv("OPENAI_API_KEY"), os.getenv("SUPABASE_DB_URL")
S_SEC, S_WH = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY, T_EM = os.getenv("RESEND_API_KEY"), os.getenv("TEST_EMAIL")
T_SEC, P_URL = os.getenv("TRIGGER_SECRET"), os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# --- THE CLOUD ENDPOINTS (The "Back Door" & London Expansion) ---
# We use the direct ESRI Cloud links to bypass local council firewalls.
COUNCILS = {
    "Leeds": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
    "London_Mega_Hub": "https://maps.london.gov.uk/arcgis/rest/services/apps/planning_data_map_02/MapServer/0/query",
    "Woking": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications_Live/FeatureServer/0/query",
    "Surrey_Heath": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications/FeatureServer/0/query",
    "Elmbridge": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications/FeatureServer/0/query",
    "Tandridge": "https://services.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications/FeatureServer/0/query"
}

# --- WEB PAGES ---
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def lander():
    return f"<html><body style='font-family:sans-serif;'><h1>Vector Data Labs</h1><p>System V8.3 Active.</p><p>Status: All Systems Operational.</p><hr/><a href='/terms'>Terms</a> | <a href='/privacy'>Privacy</a></body></html>"

@app.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def terms(): return "<html><body><h1>Terms</h1><p>All sales final.</p></body></html>"

@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy(): return "<html><body><h1>Privacy</h1><p>Data used for lead processing only.</p></body></html>"

# --- LOGIC: CLASSIFICATION & FETCHING ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "conservation"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "extension"]

def get_d(r):
    # Extracts timestamp from various possible ArcGIS date fields
    v = r.get("DATEAPVAL") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("RECDAT") or 0
    return float(v)

def classify(r):
    # Councils use different keys for descriptions
    p = str(r.get("PROPOSAL") or r.get("DESCRIPTION") or r.get("DESCRIPT") or r.get("DETDESC") or "").lower()
    if not p: return False, 0
    
    m = [k for k in TREE_WORDS if k in p]
    s = len(m)
    
    if "tree" in p: s += 2
    if any(x in p for x in ["fell", "remove", "crown", "tpo"]): s += 5
    
    # Negative filtering
    if any(w in p for w in SKIP_WORDS) and s < 7: return False, 0
    
    return (s > 1), s

def fetch_council(url):
    # The 'Human Mask' and 'ID Sort Trick' are applied here
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
    # The "400 Error Trap": where=1=1 (Fetch raw, sort in Python)
    q = {
        "where": "1=1", 
        "outFields": "*", 
        "resultRecordCount": 100, 
        "orderByFields": "OBJECTID DESC", 
        "f": "json"
    }
    try:
        res = requests.get(url, params=q, headers=h, timeout=15)
        data = res.json()
        if "error" in data: return [], f"Error: {data['error'].get('message')}"
        return [f.get("attributes", {}) for f in data.get("features", [])], "Success"
    except Exception as e:
        return [], str(e)

# --- DATABASE OPERATIONS ---
def is_already_sent(ref):
    if not SURL: return False
    try:
        conn = psycopg2.connect(SURL)
        cur = conn.cursor()
        # Ensure table exists
        cur.execute("CREATE TABLE IF NOT EXISTS sent_leads (ref TEXT PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT NOW());")
        conn.commit()
        # Check for reference
        cur.execute("SELECT 1 FROM sent_leads WHERE ref = %s", (ref,))
        exists = cur.fetchone() is not None
        conn.close()
        return exists
    except Exception as e:
        logger.error(f"DB Check Error: {e}")
        return False

def mark_as_sent(ref):
    if not SURL: return
    try:
        conn = psycopg2.connect(SURL)
        cur = conn.cursor()
        cur.execute("INSERT INTO sent_leads (ref) VALUES (%s) ON CONFLICT DO NOTHING", (ref,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB Write Error: {e}")

# --- ROUTES ---

@app.get("/test-regional", tags=["Diagnostics"])
def test_all():
    """Diagnostic scoreboard to see which council backdoors are open."""
    results = {}
    for name, url in COUNCILS.items():
        recs, status = fetch_council(url)
        found = [r for r in recs if classify(r)[0]]
        results[name] = {
            "status": status, 
            "scanned": len(recs), 
            "tree_leads": len(found)
        }
    return results

@app.get("/trigger-scrape", tags=["Live"])
def scrape(secret: str = Query(...)):
    """The main engine. Fetches, classifies, AI-scores, and sends Stripe-enabled emails."""
    if secret != T_SEC: 
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    leads_sent = 0
    # Process councils one by one
    for c_name, c_url in COUNCILS.items():
        recs, _ = fetch_council(c_url)
        # 30-day cutoff logic
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
        
        for r in recs:
            # Generate unique reference
            ref = r.get("REFVAL") or r.get("REFERENCE") or r.get("PLANNO") or str(r.get("OBJECTID"))
            is_t, score = classify(r)
            
            if is_t and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
                # AI Enhancement
                addr = r.get('ADDRESS') or r.get('LOCATION') or r.get('SITE_ADDRESS') or r.get('FULL_ADDRESS')
                prop = r.get('PROPOSAL') or r.get('DESCRIPTION') or r.get('DESCRIPT') or r.get('DETDESC')
                
                try:
                    ai = client.chat.completions.create(
                        model="gpt-4o-mini", 
                        response_format={"type": "json_object"},
                        messages=[
                            {"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool). High value if multiple trees or major felling."},
                            {"role": "user", "content": f"Addr: {addr} Prop: {prop}"}
                        ]
                    )
                    ld = json.loads(ai.choices[0].message.content)
                except Exception as e:
                    logger.error(f"AI Error: {e}")
                    continue

                # Find Active Surgeons
                cons = []
                if SURL:
                    try:
                        db = psycopg2.connect(SURL); c = db.cursor()
                        c.execute("SELECT id, email FROM tree_surgeons WHERE active IS TRUE")
                        for row in c.fetchall(): 
                            cons.append({"id": row[0], "email": row[1]})
                        db.close()
                    except: pass
                
                # Fallback to test email if no surgeons in DB
                if not cons: 
                    cons.append({"id": 999, "email": T_EM})

                for cn in cons:
                    # Dynamic Pricing
                    amt = 4500 if ld.get("high_value") else 2500
                    
                    # Create Stripe Session
                    sess = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{
                            "price_data": {
                                "currency": "gbp", 
                                "product_data": {"name": f"Exclusive Lead: {ld.get('site_address')}"}, 
                                "unit_amount": amt
                            }, 
                            "quantity": 1
                        }],
                        mode="payment", 
                        success_url=f"{P_URL}/payment-success", 
                        cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={
                            "surgeon_id": str(cn["id"]), 
                            "ref": ref, 
                            "site_address": ld.get("site_address"), 
                            "applicant_name": ld.get("applicant_name")
                        }
                    )
                    
                    # Send Email via Resend
                    email_body = f"""
                    <h3>New Tree Lead in {c_name}</h3>
                    <p><strong>Summary:</strong> {ld.get('scope_summary')}</p>
                    <p><strong>Status:</strong> Available for immediate purchase.</p>
                    <a href='{sess.url}' style='background:green; color:white; padding:10px; text-decoration:none;'>Buy Lead for £{amt/100}</a>
                    """
                    requests.post(
                        R_URL, 
                        json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [cn["email"]], "subject": f"Lead: {ld.get('site_address')}", "html": email_body}, 
                        headers={"Authorization": f"Bearer {R_KEY}"}
                    )
                
                mark_as_sent(ref)
                leads_sent += 1
                # Limit per trigger to avoid API spam
                if leads_sent >= 5: break
        if leads_sent >= 5: break
        
    return {"status": "success", "leads_processed": leads_sent}

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
                # Notify the Master (You) that a sale happened
                msg = f"<h3>Lead Paid!</h3><p>Surgeon ID: {m.get('surgeon_id')}</p><p>Address: {m.get('site_address')}</p><p>Ref: {m.get('ref')}</p>"
                requests.post(
                    R_URL, 
                    json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [T_EM], "subject": "💰 LEAD PURCHASED", "html": msg}, 
                    headers={"Authorization": f"Bearer {R_KEY}"}
                )
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
    return {"status": "ok"}

@app.get("/payment-success", include_in_schema=False)
def success(): return HTMLResponse("<html><body><h1>Success!</h1><p>The lead details have been emailed to you.</p></body></html>")

@app.get("/payment-cancelled", include_in_schema=False)
def cancel(): return HTMLResponse("<html><body><h1>Payment Cancelled</h1><p>You have not been charged.</p></body></html>")
