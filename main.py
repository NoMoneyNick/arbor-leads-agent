import os, json, logging, requests, psycopg2, stripe, urllib3, time
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Professional Stability Setup
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = FastAPI(title="Vector Data Labs - V30.0 Verified Master", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# --- ENVIRONMENT ---
OKEY, SURL = os.getenv("OPENAI_API_KEY"), os.getenv("SUPABASE_DB_URL")
S_SEC, S_WH = os.getenv("STRIPE_SECRET_KEY"), os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY, T_EM = os.getenv("RESEND_API_KEY"), os.getenv("TEST_EMAIL")
T_SEC, P_URL = os.getenv("TRIGGER_SECRET"), os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# --- THE MASTER DATA ARCHITECTURE (V30.0 Verified) ---
COUNCILS = {
    "Leeds_Control": {
        "type": "arcgis",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Official_Hub": {
        "type": "rest_api",
        # The Primary 'Front Door' for the entire capital
        "url": "https://planning.data.london.gov.uk/api/v1/applications/",
        "params": {"page_size": 100}
    },
    "London_Backup_Hub": {
        "type": "arcgis",
        # A 'Side Door' hub that often bypasses the main API firewall
        "url": "https://gis2.london.gov.uk/server/rest/services/apps/planning_data_map_02/MapServer/0/query",
        "referer": "https://www.london.gov.uk/"
    },
    "Surrey_Cluster": {
        "type": "arcgis",
        # The physical S96p server hosting Woking and surrounding councils
        "url": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications/FeatureServer/0/query",
        "referer": "https://www.woking.gov.uk/"
    }
}

# --- LOGIC: THE WIDE NET ---
# Using your logic to find the cabinet first, then the files.
HEADER_KEYWORDS = ["proposal", "description", "nature", "work", "natureofwork", "details", "devdesc", "development_description"]
TREE_SPECIFIC = ["tree", "tpo", "fell", "crown", "pruning", "oak", "ash", "sycamore", "cedar", "birch", "willow"]

def smart_classify(record):
    description = ""
    for key, value in record.items():
        if any(hk in key.lower() for hk in HEADER_KEYWORDS):
            description = str(value).lower()
            break
    if not description: return False, 0
    score = sum(2 for word in TREE_SPECIFIC if word in description)
    if "tree" in description: score += 5
    return (score > 4), score

def get_d(r):
    v = r.get("received_date") or r.get("DATE_RECEIVED") or r.get("DATE_VALID") or r.get("DATEAPVAL") or 0
    if isinstance(v, str):
        try: return datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp() * 1000
        except: return 0
    return float(v)

# --- FETCHING ENGINE (The Verified Identity V30) ---
def fetch_council(name, config):
    session = requests.Session()
    # Identifying as a legitimate research tool to establish server trust
    h = {
        "User-Agent": "VectorDataLabs/30.0 (PropTech Research Tool for Local Business; contact: admin@vectordata.labs)",
        "Accept": "application/json",
        "Referer": config.get("referer", "https://www.google.com")
    }

    try:
        if config["type"] == "arcgis":
            q = {"where": "1=1", "outFields": "*", "resultRecordCount": 100, "orderByFields": "OBJECTID DESC", "f": "json"}
            res = session.get(config["url"], params=q, headers=h, timeout=25, verify=False)
        else:
            res = session.get(config["url"], params=config.get("params"), headers=h, timeout=25)

        if res.status_code != 200: return [], f"HTTP {res.status_code}"
        if "application/json" not in res.headers.get("Content-Type", ""):
            return [], "Security Challenge (HTML)"

        data = res.json()
        if config["type"] == "arcgis":
            return [f.get("attributes", {}) for f in data.get("features", [])], "Online"
        else:
            return data.get("results", []), "Online"

    except Exception as e:
        return [], f"Fault: {str(e)}"

# --- DATABASE OPERATIONS ---
def is_already_sent(ref):
    if not SURL: return False
    try:
        conn = psycopg2.connect(SURL); cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS sent_leads (ref TEXT PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT NOW());")
        conn.commit()
        cur.execute("SELECT 1 FROM sent_leads WHERE ref = %s", (ref,))
        exists = cur.fetchone() is not None; conn.close()
        return exists
    except: return False

def mark_as_sent(ref):
    if not SURL: return
    try:
        conn = psycopg2.connect(SURL); cur = conn.cursor()
        cur.execute("INSERT INTO sent_leads (ref) VALUES (%s) ON CONFLICT DO NOTHING", (ref,))
        conn.commit(); conn.close()
    except: pass

# --- ROUTES ---
@app.get("/")
def lander():
    return f"<html><body style='font-family:sans-serif; text-align:center; padding-top:50px;'><h1>Vector Data Labs V30.0</h1><p>Verified Identity Integration Online</p><a href='/test-regional'>Run Diagnostic</a></body></html>"

@app.get("/test-regional")
def test_all():
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_council(name, config)
        found = [r for r in recs if smart_classify(r)[0]]
        results[name] = {"status": status, "records_found": len(recs), "tree_leads": len(found)}
    return results

@app.get("/trigger-scrape")
def scrape(secret: str = Query(...)):
    if secret != T_SEC: raise HTTPException(status_code=401)
    leads_sent = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    for c_name, config in COUNCILS.items():
        recs, _ = fetch_council(c_name, config)
        for r in recs:
            ref = str(r.get("external_system_reference") or r.get("REFERENCE") or r.get("OBJECTID"))
            is_valid, _ = smart_classify(r)
            if is_valid and (get_d(r) >= cutoff or get_d(r) == 0) and not is_already_sent(ref):
                addr = r.get('full_address') or r.get('ADDRESS') or r.get('LOCATION') or r.get('SITE_ADDRESS')
                prop = ""
                for k, v in r.items():
                    if any(hk in k.lower() for hk in HEADER_KEYWORDS): prop = v; break
                try:
                    ai = client.chat.completions.create(
                        model="gpt-4o-mini", response_format={"type": "json_object"},
                        messages=[{"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)."},
                        {"role": "user", "content": f"Addr: {addr} Prop: {prop}"}]
                    )
                    ld = json.loads(ai.choices[0].message.content)
                except: continue
                surgeons = [{"id": 1, "email": T_EM}]
                if SURL:
                    try:
                        db = psycopg2.connect(SURL); cur = db.cursor()
                        c.execute("SELECT id, email FROM tree_surgeons WHERE active IS TRUE")
                        surgeons = [{"id": row[0], "email": row[1]} for row in c.fetchall()] or surgeons
                        db.close()
                    except: pass
                for sgn in surgeons:
                    amt = 6000 if ld.get("high_value") else 3500
                    checkout = stripe.checkout.Session.create(
                        payment_method_types=["card"],
                        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Lead: {ld.get('site_address')}"}, "unit_amount": amt}, "quantity": 1}],
                        mode="payment", success_url=f"{P_URL}/payment-success", cancel_url=f"{P_URL}/payment-cancelled",
                        metadata={"surgeon_id": str(sgn["id"]), "ref": ref, "site_address": ld.get("site_address")}
                    )
                    email_html = f"<h2>New Lead: {c_name}</h2><p>{ld.get('scope_summary')}</p><a href='{checkout.url}'>Purchase Lead</a>"
                    requests.post(R_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [sgn["email"]], "subject": f"Lead: {ld.get('site_address')}", "html": email_html}, headers={"Authorization": f"Bearer {R_KEY}"})
                mark_as_sent(ref)
                leads_sent += 1
                if leads_sent >= 10: break
    return {"status": "success", "leads_sent": leads_sent}
