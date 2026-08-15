import os, json, logging, requests, psycopg2, stripe, urllib3, time
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Professional Stability Setup
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = FastAPI(title="Vector Data Labs - V50.0 Discovery Master", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# --- ENVIRONMENT ---
OKEY = os.getenv("OPENAI_API_KEY")
SURL = os.getenv("SUPABASE_DB_URL")
S_SEC = os.getenv("STRIPE_SECRET_KEY")
R_KEY = os.getenv("RESEND_API_KEY")
T_EM = os.getenv("TEST_EMAIL") 
T_SEC = os.getenv("TRIGGER_SECRET") 
P_URL = os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC

# --- DATA ARCHITECTURE (V50.0 Verified) ---
COUNCILS = {
    "Leeds_City_Control": {
        "type": "arcgis",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "Manchester_Arcus_Expansion": {
        "type": "aura",
        "url": "https://arcusbe.manchester.gov.uk/pr/s/sfsites/aura",
        "referer": "https://arcusbe.manchester.gov.uk/pr/s/register-view?c__r=Arcus_BE_Public_Register",
        "fwuid": "OUcwT3JDYUZld21JQ2ZOckR1VnppUWtVMjdnTGFERUU2S3FfSVdrcU92bkExNC4xOTIuODM4ODYwOA",
        "app_id": "1706_8wJLrETnpOGvg7aPJCutcg",
        "page_scope": "32c0b64d-4f6a-480c-bc4c-eb195dbfb461",
        "cookie": "renderCtx=%7B%22pageId%22%3A%22ecff068c-8aa5-4e65-a3d2-b1425f9aa8b0%22%2C%22schema%22%3A%22Published%22%2C%22viewType%22%3A%22Published%22%2C%22brandingSetId%22%3A%22fb298127-d9df-4823-9460-297f548c8719%22%2C%22audienceIds%22%3A%22%22%7D; CookieConsentPolicy=1:1; LSKey-c$CookieConsentPolicy=1:1; pctrk=b10e1434-fe88-46db-bde8-c3b88f0bf1ca"
    }
}

# --- CLASSIFICATION LOGIC ---
CABINET_HEADERS = ["proposal", "description", "development_description", "nature", "details", "PROPOSAL", "siteAddress", "address"]
TREE_GOLD = ["tree", "tpo", "fell", "felling", "arboriculture", "crown", "pruning", "stump", "oak", "ash", "willow", "cedar"]

def smart_classify(record):
    description = ""
    for key, value in record.items():
        if any(h in key.lower() for h in CABINET_HEADERS):
            description = str(value).lower()
            break
    if not description: return False, 0
    tree_matches = [word for word in TREE_GOLD if word in description]
    if tree_matches:
        score = len(tree_matches) * 5
        if any(x in description for x in ["fell", "felling", "tpo"]): score += 15
        return (score >= 10), score
    return False, 0

# --- AUTHORIZED DATA ENGINE ---
def fetch_council(name, config):
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": config["referer"]
    }
    try:
        if config["type"] == "arcgis":
            params = {"where": "1=1", "outFields": "*", "resultRecordCount": 50, "orderByFields": "OBJECTID DESC", "f": "json"}
            res = session.get(config["url"], params=params, headers=headers, timeout=15, verify=False)
            if res.status_code == 200:
                return [f.get("attributes", {}) for f in res.json().get("features", [])], "Online"

        elif config["type"] == "aura":
            headers["Cookie"] = config["cookie"]
            headers["x-sfdc-page-scope-id"] = config["page_scope"]
            
            aura_context = {"mode": "PROD", "fwuid": config["fwuid"], "app": "siteforce:communityApp", 
                            "loaded": {"APPLICATION@markup://siteforce:communityApp": config["app_id"]}}
            
            # We are now targeting 'Building_Control_Applications' as per your working cURL
            message = {"actions": [{"id": "1;a", "descriptor": "aura://ApexActionController/ACTION$execute", 
                        "params": {"namespace": "arcuscommunity", "classname": "PR_SearchService", "method": "search", 
                        "params": {"request": {"registerName": "Arcus_BE_Public_Register", "searchType": "quick", 
                        "searchTerm": "oak", "searchName": "Building_Control_Applications"}}}}]}
            
            payload = {"message": json.dumps(message), "aura.context": json.dumps(aura_context), "aura.token": "null"}
            
            res = session.post(config["url"], data=payload, headers=headers, timeout=20, verify=False)
            if res.status_code == 200:
                data = res.json()
                # Manchester (Arcus) returns a very complex structure. We need to dig deep:
                if 'actions' in data and data['actions'][0].get('state') == 'SUCCESS':
                    return data['actions'][0]['returnValue'].get('records', []), "Online"
                return [], "Online (Empty Search)"

        return [], f"Offline ({res.status_code})"
    except Exception as e:
        return [], "Connection Fault"

# --- PERSISTENCE ---
def is_already_sent(ref):
    if not SURL: return False
    try:
        conn = psycopg2.connect(SURL); cur = conn.cursor()
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
    return f"""
    <html><body style='font-family:sans-serif; text-align:center; padding-top:50px; background:#f4f4f9;'>
    <div style='display:inline-block; padding:50px; background:white; border-radius:15px; box-shadow:0 10px 30px rgba(0,0,0,0.1); border-top: 6px solid #1b5e20; max-width:600px;'>
        <h1 style='color:#1b5e20;'>Vector Data Labs</h1>
        <p>Integration Hub V50.0</p>
        <div style='background:#f1f8e9; padding:15px; border-radius:10px; margin:20px 0; text-align:left; font-size:14px;'>
            <b>Leeds:</b> Active<br/>
            <b>Manchester:</b> Target - Building Control
        </div>
        <a href='/test-regional' style='display:inline-block; padding:12px 25px; background:#1b5e20; color:white; text-decoration:none; border-radius:5px; font-weight:bold;'>Check Live Leads Feed</a>
    </div>
    </body></html>
    """

@app.get("/test-regional")
def test_all():
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_council(name, config)
        found = [r for r in recs if smart_classify(r)[0]]
        results[name] = {"status": status, "scanned": len(recs), "tree_leads": len(found)}
    return results

@app.get("/trigger-scrape")
def scrape(secret: str = Query(...)):
    if secret != T_SEC: raise HTTPException(status_code=401)
    leads_sent = 0
    for c_name, config in COUNCILS.items():
        recs, _ = fetch_council(c_name, config)
        for r in recs:
            ref = str(r.get("REFERENCE") or r.get("OBJECTID") or r.get("Id") or r.get("_id"))
            if smart_classify(r)[0] and not is_already_sent(ref):
                mark_as_sent(ref)
                leads_sent += 1
    return {"status": "success", "leads_sent": leads_sent}
