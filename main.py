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
# VECTOR DATA LABS
# REAL LEEDS COUNCIL DATA TEST VERSION
# ============================================================

app = FastAPI()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs-test")


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

LEEDS_PLANNING_URL = (
    "https://mapservices.leeds.gov.uk/"
    "arcgis/rest/services/Public/Planning/MapServer/12/query"
)

LEEDS_VALIDATIONS_URL = (
    "https://mapservices.leeds.gov.uk/"
    "arcgis/rest/services/Public/Planning/MapServer/11/query"
)

LEEDS_LAST_MONTH_URL = (
    "https://mapservices.leeds.gov.uk/"
    "arcgis/rest/services/Public/Planning/MapServer/1/query"
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


# ============================================================
# FALLBACK WEBHOOK IDEMPOTENCY STORE
# ============================================================

_processed_sessions_memory = set()


# ============================================================
# TREE KEYWORDS
# ============================================================
#
# IMPORTANT:
#
# We deliberately do NOT simply search every field for the word
# "tree".
#
# We primarily inspect the actual proposal text.
#
# This helps prevent large developments being classified as tree
# jobs just because "tree" appears somewhere in an unrelated
# address, metadata field, or underlying record.
# ============================================================

STRONG_TREE_KEYWORDS = [
    "tree",
    "trees",
    "tpo",
    "tree preservation order",
    "arboricultural",
    "arboriculture",
    "arborist",
    "tree surgeon",
    "tree surgery",
    "tree works",
    "tree work",
    "tree removal",
    "tree felling",
    "felling",
    "fell tree",
    "fell trees",
    "sectional felling",
    "sectional dismantling",
    "crown reduction",
    "crown lifting",
    "crown thinning",
    "crown raise",
    "crown raising",
    "pollard",
    "pollarding",
    "pruning",
    "prune",
    "deadwood",
    "stump grinding",
    "stump removal",
    "stump",
    "dead tree",
    "dead trees",
    "diseased tree",
    "diseased trees",
    "dangerous tree",
    "dangerous trees",
]


TREE_WORK_PHRASES = [
    "works to trees",
    "works to tree",
    "work to trees",
    "work to tree",
    "remove tree",
    "remove trees",
    "removal of tree",
    "removal of trees",
    "removal of a tree",
    "felling of tree",
    "felling of trees",
    "felling a tree",
    "fell and remove",
    "fell and remove trees",
    "pruning of tree",
    "pruning of trees",
    "pruning works",
    "tree pruning",
    "tree removal",
    "tree maintenance",
]


NON_TREE_DEVELOPMENT_WORDS = [
    "new housing development",
    "residential development",
    "erection of dwelling",
    "erection of dwellings",
    "451 dwellings",
    "900 dwellings",
    "storey residential",
    "office building",
    "commercial floorspace",
    "mixed use development",
]


# ============================================================
# GENERAL HELPERS
# ============================================================

def safe_string(value):
    if value is None:
        return ""

    return str(value).strip()


def milliseconds_to_date(value):
    if not value:
        return None

    try:
        return datetime.fromtimestamp(
            float(value) / 1000,
            tz=timezone.utc
        ).strftime("%Y-%m-%d")
    except Exception:
        return None


def get_text_for_tree_matching(record):
    """
    Use proposal as the primary source for tree matching.

    Address is deliberately excluded from the keyword search.
    """

    proposal = safe_string(
        record.get("PROPOSAL")
    ).lower()

    return proposal


def classify_tree_application(record):
    """
    Returns:
        {
            "is_tree_related": bool,
            "score": int,
            "matched_keywords": list,
            "reason": str
        }
    """

    proposal = get_text_for_tree_matching(record)

    if not proposal:
        return {
            "is_tree_related": False,
            "score": 0,
            "matched_keywords": [],
            "reason": "No proposal text available",
        }


    matched_keywords = []

    for keyword in STRONG_TREE_KEYWORDS:

        if keyword.lower() in proposal:

            matched_keywords.append(keyword)


    for phrase in TREE_WORK_PHRASES:

        if phrase.lower() in proposal:

            if phrase not in matched_keywords:
                matched_keywords.append(phrase)


    score = 0


    # Strong tree terminology
    if matched_keywords:
        score += len(matched_keywords)


    # Extra points for actual physical tree work
    physical_work_terms = [
        "fell",
        "felling",
        "remove",
        "removal",
        "prun",
        "pollard",
        "crown",
        "stump",
        "sectional",
        "dismantling",
    ]

    physical_work_count = sum(
        1
        for term in physical_work_terms
        if term in proposal
    )

    score += physical_work_count * 2


    # TPO is particularly valuable
    if "tpo" in proposal or "tree preservation order" in proposal:

        score += 5


    # If it looks like a generic development and has only a weak
    # tree reference, reject it.
    development_matches = [
        phrase
        for phrase in NON_TREE_DEVELOPMENT_WORDS
        if phrase in proposal
    ]


    if development_matches and len(matched_keywords) <= 1:

        return {
            "is_tree_related": False,
            "score": score,
            "matched_keywords": matched_keywords,
            "reason": (
                "Generic development proposal with weak "
                "tree reference"
            ),
        }


    is_tree_related = score >= 3


    if is_tree_related:

        reason = (
            "Proposal contains specific tree/tree-work terminology"
        )

    else:

        reason = (
            "Insufficient evidence of a tree-related job"
        )


    return {
        "is_tree_related": is_tree_related,
        "score": score,
        "matched_keywords": matched_keywords,
        "reason": reason,
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
def health_check():

    missing = [
        name
        for name, value in REQUIRED_ENV_VARS.items()
        if not value
    ]

    return {
        "status": "Vector Data Labs Agent Active",
        "mode": "REAL LEEDS DATA TEST",
        "region": "Frankfurt Hub",
        "missing_env_vars": missing,
        "public_app_url_set": bool(PUBLIC_APP_URL),
    }


# ============================================================
# STRIPE REDIRECT PAGES
# ============================================================

@app.get("/payment-success")
def payment_success():

    return {
        "status": "SUCCESS",
        "message": "Stripe test payment completed."
    }


@app.get("/payment-cancelled")
def payment_cancelled():

    return {
        "status": "CANCELLED",
        "message": "Stripe test payment cancelled."
    }


# ============================================================
# EMAIL FUNCTION
# ============================================================

def send_test_email(
    recipient,
    subject,
    body,
    sender_name="Vector Data Labs Test"
):

    if not RESEND_API_KEY:

        raise RuntimeError(
            "RESEND_API_KEY is not configured"
        )


    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }


    payload = {
        "from": f"{sender_name} <onboarding@resend.dev>",
        "to": [recipient],
        "subject": subject,
        "text": body,
    }


    response = requests.post(
        RESEND_URL,
        json=payload,
        headers=headers,
        timeout=10,
    )


    logger.info(
        "Resend response: HTTP %s",
        response.status_code
    )


    if not response.ok:

        logger.error(
            "Resend failed: %s",
            response.text
        )

        raise RuntimeError(
            f"Resend returned HTTP {response.status_code}"
        )


    try:

        return response.json()

    except Exception:

        return {
            "status": "sent",
            "http_status": response.status_code,
        }


# ============================================================
# GET TEST CONTRACTORS
# ============================================================

def get_test_contractors():

    contractors = []


    if SUPABASE_DB_URL:

        conn = None

        try:

            conn = psycopg2.connect(
                SUPABASE_DB_URL,
                connect_timeout=10
            )


            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT id, business_name, email
                    FROM tree_surgeons
                    WHERE active IS NOT FALSE;
                    """
                )

                rows = cur.fetchall()


            for contractor_id, name, email in rows:

                contractors.append({

                    "id": contractor_id,

                    "name": name,

                    "email": email,

                    "distance": 4.2,
                })


            logger.info(
                "Database returned %s contractors",
                len(contractors)
            )


        except Exception as exc:

            logger.warning(
                "Database unavailable during test: %s",
                exc
            )


        finally:

            if conn:

                conn.close()


    if not contractors:

        logger.info(
            "Using TEST CONTRACTOR"
        )

        contractors.append({

            "id": 1,

            "name": "Test Contractor",

            "email": TEST_EMAIL,

            "distance": 4.2,
        })


    return contractors


# ============================================================
# WEBHOOK IDEMPOTENCY
# ============================================================

def mark_session_processed(session_id: str) -> bool:

    """
    Returns True the first time a Stripe session id is seen.

    Returns False if the same session has already been processed.
    """

    if SUPABASE_DB_URL:

        conn = None

        try:

            conn = psycopg2.connect(
                SUPABASE_DB_URL,
                connect_timeout=10
            )


            with conn.cursor() as cur:

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                    processed_stripe_events (
                        session_id TEXT PRIMARY KEY,
                        processed_at TIMESTAMPTZ
                        DEFAULT now()
                    );
                    """
                )


                cur.execute(
                    """
                    INSERT INTO processed_stripe_events
                    (session_id)
                    VALUES (%s)
                    ON CONFLICT (session_id)
                    DO NOTHING
                    RETURNING session_id;
                    """,
                    (session_id,),
                )


                row = cur.fetchone()


            conn.commit()


            return row is not None


        except Exception as exc:

            logger.warning(
                "DB idempotency check failed, "
                "falling back to memory: %s",
                exc
            )


        finally:

            if conn:

                conn.close()


    if session_id in _processed_sessions_memory:

        return False


    _processed_sessions_memory.add(session_id)

    return True


# ============================================================
# LEEDS COUNCIL API
# ============================================================

def fetch_leeds_records(
    max_records=1000
):

    """
    Download the newest Leeds planning records.

    We use the main Planning Applications layer because it
    contains the proposal and address information we need.

    The query is ordered by DATEAPVAL descending so the newest
    records are considered first.
    """

    params = {

        "where": "1=1",

        "outFields": "*",

        "returnGeometry": "false",

        "resultRecordCount": max_records,

        "orderByFields": "DATEAPVAL DESC",

        "f": "json",
    }


    logger.info(
        "Requesting real Leeds Council planning data"
    )


    response = requests.get(
        LEEDS_PLANNING_URL,
        params=params,
        timeout=30,
    )


    response.raise_for_status()


    data = response.json()


    if "error" in data:

        raise RuntimeError(
            f"Leeds API error: {data['error']}"
        )


    features = data.get(
        "features",
        []
    )


    records = [

        feature.get(
            "attributes",
            {}
        )

        for feature in features

    ]


    logger.info(
        "Leeds returned %s records",
        len(records)
    )


    return records


# ============================================================
# LEEDS RECENT RECORD FILTER
# ============================================================

def filter_recent_records(
    records,
    days=60
):

    """
    Keep records whose validation/approval date is recent.

    Leeds exposes DATEAPVAL in the main planning layer.

    This gives us a safety filter so old historical planning
    records do not become leads.
    """

    cutoff = datetime.now(
        timezone.utc
    ) - timedelta(
        days=days
    )


    cutoff_ms = int(
        cutoff.timestamp() * 1000
    )


    recent = []


    for record in records:

        value = record.get(
            "DATEAPVAL"
        )


        if not value:

            continue


        try:

            timestamp = float(
                value
            )

        except Exception:

            continue


        if timestamp >= cutoff_ms:

            recent.append(
                record
            )


    return recent


# ============================================================
# TREE APPLICATION FILTER
# ============================================================

def find_tree_applications(
    records
):

    tree_applications = []


    for record in records:

        classification = classify_tree_application(
            record
        )


        if not classification["is_tree_related"]:

            continue


        enriched = dict(
            record
        )


        enriched["matched_keywords"] = (
            classification["matched_keywords"]
        )


        enriched["tree_match_score"] = (
            classification["score"]
        )


        enriched["tree_match_reason"] = (
            classification["reason"]
        )


        enriched["DATEDECISS_readable"] = (
            milliseconds_to_date(
                record.get("DATEDECISS")
            )
        )


        enriched["DATEAPVAL_readable"] = (
            milliseconds_to_date(
                record.get("DATEAPVAL")
            )
        )


        tree_applications.append(
            enriched
        )


    tree_applications.sort(
        key=lambda item:
            item.get(
                "tree_match_score",
                0
            ),
        reverse=True
    )


    return tree_applications


# ============================================================
# OPENAI LEAD EXTRACTION
# ============================================================

def extract_lead_with_openai(
    record
):

    proposal = safe_string(
        record.get("PROPOSAL")
    )


    address = safe_string(
        record.get("ADDRESS")
    )


    reference = safe_string(
        record.get("REFVAL")
    )


    applicant = safe_string(
        record.get("APPNAME")
    )


    if not applicant:

        applicant = "Not supplied by Leeds GIS record"


    raw_text = f"""
Leeds City Council planning application.

Application reference:
{reference}

Applicant:
{applicant}

Site address:
{address}

Proposal:
{proposal}
"""


    system_prompt = """

You are a UK planning-data extraction system.

Extract the information from the supplied Leeds Council
planning application.

Return ONLY valid JSON with exactly these fields:

{
  "applicant_name": "string",
  "site_address": "string",
  "postcode": "string",
  "scope_summary": "string",
  "high_value": true
}

Rules:

1. Do not invent information.

2. Extract the postcode from the site address if present.

3. If there is no postcode, return an empty string.

4. Keep the scope_summary focused on the actual tree work.

5. high_value should be true where the work appears to involve
   substantial tree work such as:
   - multiple mature trees
   - sectional felling
   - dangerous trees
   - major removals
   - stump grinding/removal
   - TPO work
   - significant crown work

6. Do not classify an ordinary housing development as a
   high-value tree job merely because trees are mentioned.

"""


    try:

        response = openai_client.chat.completions.create(

            model="gpt-4o-mini",

            response_format={
                "type": "json_object"
            },

            messages=[

                {
                    "role": "system",
                    "content": system_prompt,
                },

                {
                    "role": "user",
                    "content": raw_text,
                },

            ],

            temperature=0.1,
        )


        raw_content = (
            response
            .choices[0]
            .message
            .content
        )


        if not raw_content:

            raise RuntimeError(
                "OpenAI returned no content"
            )


        result = json.loads(
            raw_content
        )


        return result


    except Exception as exc:

        logger.exception(
            "OpenAI extraction failed: %s",
            exc
        )


        # Fall back to deterministic extraction rather than
        # losing the real council record.

        postcode = ""


        address_parts = address.split()


        for index, part in enumerate(
            address_parts
        ):

            if part.upper() in [
                "LS1",
                "LS2",
                "LS3",
                "LS4",
                "LS5",
                "LS6",
                "LS7",
                "LS8",
                "LS9",
                "LS10",
                "LS11",
                "LS12",
                "LS13",
                "LS14",
                "LS15",
                "LS16",
                "LS17",
                "LS18",
                "LS19",
                "LS20",
                "LS21",
                "LS22",
                "LS23",
                "LS24",
                "LS25",
                "LS26",
                "LS27",
                "LS28",
                "LS29",
            ]:

                postcode = (
                    " ".join(
                        address_parts[
                            index:index + 2
                        ]
                    )
                )

                break


        classification = classify_tree_application(
            record
        )


        return {

            "applicant_name":
                applicant,

            "site_address":
                address,

            "postcode":
                postcode,

            "scope_summary":
                proposal,

            "high_value":
                classification["score"] >= 6,
        }


# ============================================================
# CREATE STRIPE TEST CHECKOUT
# ============================================================

def create_test_checkout(
    contractor,
    lead,
    application_reference
):

    unit_amount = (
        4500
        if lead["high_value"]
        else 2500
    )


    session = stripe.checkout.Session.create(

        payment_method_types=[
            "card"
        ],


        line_items=[

            {
                "price_data": {

                    "currency": "gbp",

                    "product_data": {

                        "name":
                            (
                                "TEST - Exclusive Tree Lead - "
                                f"{lead['postcode'] or application_reference}"
                            ),
                    },

                    "unit_amount":
                        unit_amount,
                },

                "quantity":
                    1,
            }

        ],


        mode="payment",


        success_url=(

            f"{PUBLIC_APP_URL}/payment-success"

            if PUBLIC_APP_URL

            else "https://example.com"
        ),


        cancel_url=(

            f"{PUBLIC_APP_URL}/payment-cancelled"

            if PUBLIC_APP_URL

            else "https://example.com"
        ),


        metadata={

            "surgeon_id":
                str(contractor["id"]),

            "postcode":
                lead["postcode"],

            "applicant_name":
                lead["applicant_name"],

            "site_address":
                lead["site_address"],

            "scope_summary":
                lead["scope_summary"],

            "application_reference":
                application_reference,

            "test_mode":
                "true",
        },
    )


    return session


# ============================================================
# SEND TREE LEAD PAYMENT EMAIL
# ============================================================

def send_tree_lead_email(
    contractor,
    lead,
    application_reference,
    session,
    tree_record
):

    email_body = (

        f"Hello {contractor['name']},\n\n"

        "REAL LEEDS COUNCIL DATA TEST\n"
        "============================\n\n"

        "This lead was identified from the public "
        "Leeds City Council planning data feed.\n\n"

        "THIS IS STILL A TEST SYSTEM.\n"
        "No live lead purchase has been made.\n\n"

        f"APPLICATION REFERENCE:\n"
        f"{application_reference}\n\n"

        f"SITE ADDRESS:\n"
        f"{lead['site_address']}\n\n"

        f"POSTCODE:\n"
        f"{lead['postcode']}\n\n"

        f"APPLICANT:\n"
        f"{lead['applicant_name']}\n\n"

        f"WORK SCOPE:\n"
        f"{lead['scope_summary']}\n\n"

        f"TREE MATCH KEYWORDS:\n"
        f"{', '.join(tree_record.get('matched_keywords', []))}\n\n"

        f"TREE MATCH SCORE:\n"
        f"{tree_record.get('tree_match_score', 0)}\n\n"

        f"APPROXIMATE DISTANCE:\n"
        f"{contractor['distance']} miles\n\n"

        "TEST PAYMENT LINK:\n"
        f"{session.url}\n\n"

        "The payment link is part of the Stripe "
        "testing workflow.\n\n"

        "If this were live, payment would unlock "
        "the lead information.\n\n"

        "Vector Data Labs Test System"
    )


    return send_test_email(

        recipient=
            contractor["email"],

        subject=(
            "[TEST] Leeds Tree Lead - "
            f"{lead['postcode'] or application_reference}"
        ),

        body=email_body,

        sender_name=
            "Vector Data Labs Test",
    )


# ============================================================
# REAL LEEDS DATA TEST
# ============================================================

@app.get("/test-leeds")
def test_leeds():

    logger.info(
        "REAL LEEDS DATA TEST STARTED"
    )


    try:

        records = fetch_leeds_records(
            max_records=1000
        )


        recent_records = filter_recent_records(
            records,
            days=60
        )


        tree_applications = find_tree_applications(
            recent_records
        )


        return {

            "status":
                "SUCCESS",

            "source":
                "Leeds City Council",

            "test_mode":
                True,

            "records_downloaded":
                len(records),

            "recent_records_found":
                len(recent_records),

            "tree_related_records_found":
                len(tree_applications),

            "lookback_days":
                60,

            "tree_applications":
                tree_applications[:50],
        }


    except Exception as exc:

        logger.exception(
            "Leeds data test failed"
        )


        raise HTTPException(

            status_code=500,

            detail=(
                "Leeds data test failed: "
                f"{str(exc)}"
            )
        )


# ============================================================
# REAL LEEDS TREE LEAD PIPELINE
# ============================================================

@app.get("/trigger-scrape")
def trigger_scrape(
    x_trigger_secret: str = Header(default=None)
):

    if (
        not TRIGGER_SECRET
        or x_trigger_secret != TRIGGER_SECRET
    ):

        raise HTTPException(

            status_code=401,

            detail=
                "Missing or invalid trigger secret"
        )


    logger.info(
        "REAL LEEDS TREE LEAD PIPELINE STARTED"
    )


    # ========================================================
    # 1. DOWNLOAD REAL LEEDS DATA
    # ========================================================

    try:

        records = fetch_leeds_records(
            max_records=1000
        )

    except Exception as exc:

        logger.exception(
            "Failed downloading Leeds data"
        )

        raise HTTPException(

            status_code=500,

            detail=(
                "Failed downloading Leeds data: "
                f"{str(exc)}"
            )
        )


    # ========================================================
    # 2. RECENT APPLICATIONS
    # ========================================================

    recent_records = filter_recent_records(
        records,
        days=60
    )


    logger.info(
        "Recent Leeds records: %s",
        len(recent_records)
    )


    # ========================================================
    # 3. FIND TREE APPLICATIONS
    # ========================================================

    tree_applications = find_tree_applications(
        recent_records
    )


    logger.info(
        "Tree-related Leeds records: %s",
        len(tree_applications)
    )


    # ========================================================
    # 4. STOP IF NOTHING FOUND
    # ========================================================

    if not tree_applications:

        logger.info(
            "No suitable tree applications found"
        )


        return {

            "status":
                "NO TREE LEADS FOUND",

            "mode":
                "REAL LEEDS DATA TEST",

            "records_downloaded":
                len(records),

            "recent_records_found":
                len(recent_records),

            "tree_related_records_found":
                0,

            "message":
                (
                    "Leeds data was successfully downloaded, "
                    "but no recent records passed the tree "
                    "work filter."
                ),
        }


    # ========================================================
    # 5. SELECT BEST TREE LEAD
    # ========================================================

    selected_record = tree_applications[0]


    application_reference = safe_string(
        selected_record.get("REFVAL")
    )


    logger.info(
        "Selected Leeds application: %s",
        application_reference
    )


    # ========================================================
    # 6. EXTRACT LEAD INFORMATION
    # ========================================================

    lead = extract_lead_with_openai(
        selected_record
    )


    logger.info(
        "Extracted real Leeds lead: %s",
        lead
    )


    # ========================================================
    # 7. GET CONTRACTORS
    # ========================================================

    contractors = get_test_contractors()


    contractor_results = []


    # ========================================================
    # 8. CREATE TEST PAYMENT LINK
    # ========================================================

    for contractor in contractors:

        try:

            session = create_test_checkout(

                contractor=
                    contractor,

                lead=
                    lead,

                application_reference=
                    application_reference,
            )


            send_tree_lead_email(

                contractor=
                    contractor,

                lead=
                    lead,

                application_reference=
                    application_reference,

                session=
                    session,

                tree_record=
                    selected_record,
            )


            contractor_results.append({

                "contractor_id":
                    contractor["id"],

                "status":
                    "payment_link_sent",

                "stripe_session_created":
                    True,

                "application_reference":
                    application_reference,
            })


            logger.info(
                "Real Leeds test lead sent to contractor %s",
                contractor["id"]
            )


        except Exception as exc:

            logger.exception(
                "Failed sending Leeds test lead"
            )


            contractor_results.append({

                "contractor_id":
                    contractor["id"],

                "status":
                    "failed",

                "error":
                    str(exc),
            })


    # ========================================================
    # 9. RETURN RESULT
    # ========================================================

    return {

        "status":
            "REAL LEEDS TEST PIPELINE COMPLETED",

        "mode":
            "REAL LEEDS DATA TEST",

        "records_downloaded":
            len(records),

        "recent_records_found":
            len(recent_records),

        "tree_related_records_found":
            len(tree_applications),

        "selected_application":
            selected_record,

        "lead_extracted":
            lead,

        "contractors":
            contractor_results,

        "warning":
            (
                "This is still TEST mode. "
                "The Leeds data is real/public data, "
                "but Stripe and email remain test workflow."
            ),
    }


# ============================================================
# STRIPE TEST WEBHOOK
# ============================================================

@app.post("/webhook")
async def stripe_webhook(
    request: Request
):

    logger.info(
        "STRIPE WEBHOOK RECEIVED"
    )


    # ========================================================
    # 1. READ RAW STRIPE REQUEST
    # ========================================================

    payload = await request.body()


    signature = request.headers.get(
        "stripe-signature"
    )


    # ========================================================
    # 2. CHECK WEBHOOK SECRET
    # ========================================================

    if not STRIPE_WEBHOOK_SECRET:

        logger.error(
            "STRIPE_WEBHOOK_SECRET not configured"
        )


        raise HTTPException(

            status_code=500,

            detail=
                "Stripe webhook secret missing"
        )


    # ========================================================
    # 3. CHECK SIGNATURE
    # ========================================================

    if not signature:

        logger.error(
            "Stripe signature missing"
        )


        raise HTTPException(

            status_code=400,

            detail=
                "Stripe signature missing"
        )


    # ========================================================
    # 4. VERIFY STRIPE EVENT
    # ========================================================

    try:

        event = stripe.Webhook.construct_event(

            payload,

            signature,

            STRIPE_WEBHOOK_SECRET
        )


    except ValueError:

        logger.exception(
            "Invalid Stripe webhook payload"
        )


        raise HTTPException(

            status_code=400,

            detail=
                "Invalid webhook payload"
        )


    except stripe.StripeError:

        logger.exception(
            "Invalid Stripe webhook signature"
        )


        raise HTTPException(

            status_code=400,

            detail=
                "Invalid Stripe webhook signature"
        )


    logger.info(
        "Stripe event received: %s",
        event["type"]
    )


    # ========================================================
    # 5. IGNORE EVENTS WE DON'T NEED
    # ========================================================

    if event["type"] != (
        "checkout.session.completed"
    ):

        logger.info(
            "Ignoring Stripe event type: %s",
            event["type"]
        )


        return {
            "status":
                "Event ignored"
        }


    # ========================================================
    # 6. GET CHECKOUT SESSION
    # ========================================================

    session = event["data"]["object"]


    session_id = session["id"]


    # ========================================================
    # 7. GET METADATA
    # ========================================================

    metadata = session["metadata"]


    if "surgeon_id" in metadata:

        surgeon_id = metadata["surgeon_id"]

    else:

        surgeon_id = None


    if "postcode" in metadata:

        postcode = metadata["postcode"]

    else:

        postcode = "Unknown"


    if "site_address" in metadata:

        site_address = metadata["site_address"]

    else:

        site_address = "Unknown"


    if "applicant_name" in metadata:

        applicant_name = metadata["applicant_name"]

    else:

        applicant_name = "Unknown"


    if "scope_summary" in metadata:

        scope_summary = metadata["scope_summary"]

    else:

        scope_summary = "Unknown"


    if "application_reference" in metadata:

        application_reference = (
            metadata["application_reference"]
        )

    else:

        application_reference = "Unknown"


    # ========================================================
    # 8. LOG PAYMENT
    # ========================================================

    logger.info(
        "Stripe checkout completed: %s",
        session_id
    )


    logger.info(
        "Surgeon ID: %s",
        surgeon_id
    )


    logger.info(
        "Postcode: %s",
        postcode
    )


    logger.info(
        "Application reference: %s",
        application_reference
    )


    # ========================================================
    # 9. PREVENT DUPLICATES
    # ========================================================

    if not mark_session_processed(
        session_id
    ):

        logger.info(
            "Session %s already processed",
            session_id
        )


        return {

            "status":
                "Duplicate event ignored",

            "session_id":
                session_id,
        }


    # ========================================================
    # 10. PAYMENT CONFIRMED
    # ========================================================

    logger.info(
        "TEST PAYMENT COMPLETED for surgeon %s",
        surgeon_id
    )


    # ========================================================
    # 11. SEND UNLOCK EMAIL
    # ========================================================

    recipient = TEST_EMAIL


    unlock_body = (

        "★ TEST LEAD PURCHASE SUCCESSFUL ★\n\n"

        "This confirms that the Stripe payment "
        "and webhook workflow is working with "
        "a REAL Leeds Council planning record.\n\n"

        "UNLOCKED TEST LEAD\n"
        "===================\n\n"

        f"APPLICATION REFERENCE:\n"
        f"{application_reference}\n\n"

        f"SITE ADDRESS:\n"
        f"{site_address}\n\n"

        f"POSTCODE:\n"
        f"{postcode}\n\n"

        f"APPLICANT NAME:\n"
        f"{applicant_name}\n\n"

        f"WORK SCOPE:\n"
        f"{scope_summary}\n\n"

        f"TEST CONTRACTOR ID:\n"
        f"{surgeon_id}\n\n"

        "The source record came from Leeds City Council.\n\n"

        "This is TEST DATA and not a live paid lead.\n\n"

        "Vector Data Labs Test System"
    )


    # ========================================================
    # 12. SEND EMAIL
    # ========================================================

    try:

        send_test_email(

            recipient=
                recipient,

            subject=(
                "[TEST UNLOCKED] Leeds Tree Lead - "
                f"{postcode}"
            ),

            body=
                unlock_body,

            sender_name=
                "Vector Data Labs Test",
        )


    except Exception:

        logger.exception(
            "Failed sending unlocked test email"
        )


        raise HTTPException(

            status_code=500,

            detail=(
                "Payment received but "
                "test email failed"
            )
        )


    # ========================================================
    # 13. SUCCESS
    # ========================================================

    logger.info(
        "TEST PAYMENT SUCCESSFULLY PROCESSED "
        "for surgeon %s",
        surgeon_id
    )


    return {

        "status":
            "TEST PAYMENT PROCESSED",

        "session_id":
            session_id,

        "surgeon_id":
            surgeon_id,

        "recipient":
            recipient,

        "postcode":
            postcode,

        "application_reference":
            application_reference,
    }
