import os
import json
import logging
from datetime import datetime, timezone

import requests
import psycopg2
from fastapi import FastAPI, Request, HTTPException, Header
from openai import OpenAI
import stripe


# ============================================================
# TESTING VERSION
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


# Fallback store for webhook idempotency.
_processed_sessions_memory = set()


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
        "mode": "TESTING",
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
    sender_name="Vector Data Labs"
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
            "http_status": response.status_code
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
                    CREATE TABLE IF NOT EXISTS processed_stripe_events (
                        session_id TEXT PRIMARY KEY,
                        processed_at TIMESTAMPTZ DEFAULT now()
                    );
                    """
                )

                cur.execute(
                    """
                    INSERT INTO processed_stripe_events (session_id)
                    VALUES (%s)
                    ON CONFLICT (session_id) DO NOTHING
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
# TEST PIPELINE
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
            detail="Missing or invalid trigger secret"
        )

    logger.info(
        "TEST PIPELINE STARTED"
    )


    # ========================================================
    # 1. MOCK COUNCIL DATA
    # ========================================================

    mock_leeds_raw = (
        "Application Ref: 26/0814/TR. "
        "Site: 12 Headingley Lane, Leeds, LS6 2AS. "
        "Proposal: T1 & T2 Mature Oak Trees - Complete "
        "sectional felling and stump grinding due to "
        "severe structural root decay threatening boundary wall. "
        "Applicant: Mr. Julian Vance."
    )


    # ========================================================
    # 2. OPENAI EXTRACTION
    # ========================================================

    system_prompt = (
        "You are a senior UK Planning Data Engineer.\n\n"
        "Analyze raw text from council planning portals.\n\n"
        "Extract:\n"
        "- Applicant Name\n"
        "- Site Address\n"
        "- Postcode\n"
        "- Scope Summary\n\n"
        "If work involves major felling, sectional "
        "takedowns, or mature TPO trees, set high_value "
        "to true.\n\n"
        "Return strictly JSON matching this structure:\n\n"
        "{\n"
        '  "applicant_name": "String",\n'
        '  "site_address": "String",\n'
        '  "postcode": "String",\n'
        '  "scope_summary": "String",\n'
        '  "high_value": true\n'
        "}"
    )


    try:

        response = openai_client.chat.completions.create(

            model="gpt-4o-mini",

            response_format={
                "type": "json_object"
            },

            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": mock_leeds_raw
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

        refined_lead = json.loads(
            raw_content
        )

    except Exception:

        logger.exception(
            "OpenAI extraction failed"
        )

        raise HTTPException(
            status_code=500,
            detail="OpenAI extraction failed"
        )


    # ========================================================
    # 3. CHECK EXTRACTED DATA
    # ========================================================

    required_fields = [
        "applicant_name",
        "site_address",
        "postcode",
        "scope_summary",
        "high_value",
    ]

    for field in required_fields:

        if field not in refined_lead:

            raise HTTPException(
                status_code=500,
                detail=f"OpenAI output missing: {field}"
            )

    logger.info(
        "Lead extracted: %s",
        refined_lead
    )


    # ========================================================
    # 4. GET TEST CONTRACTORS
    # ========================================================

    contractors = get_test_contractors()


    # ========================================================
    # 5. SEND PAYMENT LINKS
    # ========================================================

    unit_amount = (
        4500
        if refined_lead["high_value"]
        else 2500
    )

    contractor_results = []


    for contractor in contractors:

        try:

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
                                    f"TEST - Exclusive Tree Lead - "
                                    f"{refined_lead['postcode']}",
                            },

                            "unit_amount": unit_amount,
                        },

                        "quantity": 1,
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
                        refined_lead["postcode"],

                    "applicant_name":
                        refined_lead["applicant_name"],

                    "site_address":
                        refined_lead["site_address"],

                    "scope_summary":
                        refined_lead["scope_summary"],
                },
            )


            email_body = (

                f"Hello {contractor['name']},\n\n"

                "TEST LEAD ALERT\n"
                "================\n\n"

                "This is a TEST of the "
                "Vector Data Labs lead system.\n\n"

                f"Approximate distance: "
                f"{contractor['distance']} miles\n\n"

                f"WORK SCOPE:\n"
                f"{refined_lead['scope_summary']}\n\n"

                f"POSTCODE:\n"
                f"{refined_lead['postcode']}\n\n"

                "TEST PAYMENT LINK:\n"

                f"{session.url}\n\n"

                "The payment link is part of "
                "the Stripe testing workflow.\n\n"

                "Best regards,\n"
                "Vector Data Labs Test System"
            )


            send_test_email(

                recipient=contractor["email"],

                subject=(
                    f"[TEST] Tree Lead - "
                    f"{refined_lead['postcode']}"
                ),

                body=email_body,

                sender_name="Vector Data Labs Test",
            )


            contractor_results.append({

                "contractor_id":
                    contractor["id"],

                "status":
                    "payment_link_sent",

                "stripe_session_created":
                    True,
            })


            logger.info(
                "Test payment link sent to contractor %s",
                contractor["id"]
            )


        except Exception as exc:

            logger.exception(
                "Failed contractor test"
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
    # 6. RETURN TEST RESULT
    # ========================================================

    return {

        "status":
            "TEST PIPELINE COMPLETED",

        "mode":
            "TESTING",

        "lead_extracted":
            refined_lead,

        "contractors":
            contractor_results,
    }


# ============================================================
# STRIPE TEST WEBHOOK
# ============================================================

@app.post("/webhook")
async def stripe_webhook(request: Request):

    logger.info(
        "STRIPE WEBHOOK RECEIVED"
    )


    payload = await request.body()

    signature = request.headers.get(
        "stripe-signature"
    )


    if not STRIPE_WEBHOOK_SECRET:

        logger.error(
            "STRIPE_WEBHOOK_SECRET not configured"
        )

        raise HTTPException(
            status_code=500,
            detail="Stripe webhook secret missing"
        )


    if not signature:

        logger.error(
            "Stripe signature missing"
        )

        raise HTTPException(
            status_code=400,
            detail="Stripe signature missing"
        )


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
            detail="Invalid webhook payload"
        )

    except stripe.StripeError:

        logger.exception(
            "Invalid Stripe webhook signature"
        )

        raise HTTPException(
            status_code=400,
            detail="Invalid Stripe webhook signature"
        )


    logger.info(
        "Stripe event received: %s",
        event["type"]
    )


    if event["type"] != "checkout.session.completed":

        logger.info(
            "Ignoring Stripe event type: %s",
            event["type"]
        )

        return {
            "status": "Event ignored"
        }


    session = event["data"]["object"]

    session_id = session["id"]

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


    if not mark_session_processed(session_id):

        logger.info(
            "Session %s already processed, "
            "skipping duplicate webhook",
            session_id
        )

        return {
            "status":
                "Duplicate event ignored",

            "session_id":
                session_id,
        }


    logger.info(
        "TEST PAYMENT COMPLETED for surgeon %s",
        surgeon_id
    )


    recipient = TEST_EMAIL


    unlock_body = (

        "★ TEST LEAD PURCHASE SUCCESSFUL ★\n\n"

        "This confirms that the Stripe payment "
        "and webhook workflow is working.\n\n"

        "UNLOCKED TEST LEAD\n"
        "===================\n\n"

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

        "This is TEST DATA and not a live lead.\n\n"

        "Vector Data Labs Test System"
    )


    try:

        send_test_email(

            recipient=recipient,

            subject=(
                f"[TEST UNLOCKED] "
                f"Tree Lead - {postcode}"
            ),

            body=unlock_body,

            sender_name="Vector Data Labs Test",
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
    }


# ============================================================
# REAL LEEDS COUNCIL DATA TEST
# ============================================================

@app.get("/test-leeds")
def test_leeds():

    logger.info(
        "REAL LEEDS DATA TEST STARTED"
    )


    # --------------------------------------------------------
    # Leeds City Council ArcGIS endpoint
    # --------------------------------------------------------
# ============================================================
# REAL LEEDS COUNCIL TREE APPLICATION TEST
# ============================================================

@app.get("/test-leeds")
def test_leeds():

    logger.info("REAL LEEDS TREE DATA TEST STARTED")

    url = (
        "https://mapservices.leeds.gov.uk/"
        "arcgis/rest/services/Public/Planning/"
        "MapServer/12/query"
    )

    # --------------------------------------------------------
    # TREE-WORK KEYWORDS
    # --------------------------------------------------------
    #
    # We deliberately search the PROPOSAL field rather than
    # simply searching every field.
    #
    # This helps prevent unrelated applications being labelled
    # as tree jobs just because the word "tree" appears
    # somewhere else in the council data.
    #

    tree_keywords = [
        "tree",
        "trees",
        "tree work",
        "tree works",
        "tree removal",
        "tree removals",
        "tree felling",
        "felling",
        "fell",
        "pruning",
        "prune",
        "crown reduction",
        "crown reduce",
        "crown lift",
        "crown lifting",
        "dead tree",
        "dead trees",
        "arboricultural",
        "arboriculture",
        "tpo",
        "tree preservation",
        "section 211",
        "s211",
    ]

    # --------------------------------------------------------
    # GET RECENT LEEDS APPLICATIONS
    # --------------------------------------------------------

    params = {
        "where": "1=1",
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": 1000,
        "orderByFields": "DATEAPVAL DESC",
        "f": "json",
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

    except Exception as exc:

        logger.exception(
            "Failed connecting to Leeds Council"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Leeds Council connection failed: {str(exc)}",
        )

    # --------------------------------------------------------
    # CHECK LEEDS RESPONSE
    # --------------------------------------------------------

    if "error" in data:

        logger.error(
            "Leeds API returned an error: %s",
            data["error"],
        )

        raise HTTPException(
            status_code=500,
            detail=f"Leeds API error: {data['error']}",
        )

    features = data.get("features", [])

    logger.info(
        "Leeds returned %s records",
        len(features),
    )

    # --------------------------------------------------------
    # SEARCH PROPOSALS FOR TREE WORK
    # --------------------------------------------------------

    tree_applications = []

    for feature in features:

        attributes = feature.get("attributes", {})

        proposal = attributes.get("PROPOSAL") or ""

        address = attributes.get("ADDRESS") or ""

        reference = attributes.get("REFVAL") or ""

        searchable_text = proposal.lower()

        matched_keywords = []

        for keyword in tree_keywords:

            if keyword.lower() in searchable_text:

                matched_keywords.append(keyword)

        # ----------------------------------------------------
        # ONLY KEEP APPLICATIONS WHERE THE PROPOSAL ITSELF
        # CONTAINS A TREE-RELATED TERM
        # ----------------------------------------------------

        if not matched_keywords:

            continue

        # ----------------------------------------------------
        # DATE CONVERSION
        # ----------------------------------------------------

        date_applied = attributes.get("DATEAPVAL")

        date_decided = attributes.get("DATEDECISS")

        date_applied_readable = None
        date_decided_readable = None

        if date_applied:

            try:

                from datetime import datetime, timezone

                date_applied_readable = (
                    datetime.fromtimestamp(
                        date_applied / 1000,
                        tz=timezone.utc,
                    ).strftime("%Y-%m-%d")
                )

            except Exception:

                date_applied_readable = None

        if date_decided:

            try:

                from datetime import datetime, timezone

                date_decided_readable = (
                    datetime.fromtimestamp(
                        date_decided / 1000,
                        tz=timezone.utc,
                    ).strftime("%Y-%m-%d")
                )

            except Exception:

                date_decided_readable = None

        # ----------------------------------------------------
        # ADD MATCH
        # ----------------------------------------------------

        tree_applications.append({

            "OBJECTID":
                attributes.get("OBJECTID"),

            "REFVAL":
                reference,

            "PROPOSAL":
                proposal,

            "DTYPNUMBCO":
                attributes.get("DTYPNUMBCO"),

            "DATEDECISS":
                date_decided,

            "DATEAPVAL":
                date_applied,

            "DATEDECISS_readable":
                date_decided_readable,

            "DATEAPVAL_readable":
                date_applied_readable,

            "DCSTAT":
                attributes.get("DCSTAT"),

            "DECSN":
                attributes.get("DECSN"),

            "DCAPPTYP":
                attributes.get("DCAPPTYP"),

            "ADDRESS":
                address,

            "matched_keywords":
                matched_keywords,
        })

    # --------------------------------------------------------
    # RETURN RESULTS
    # --------------------------------------------------------

    logger.info(
        "Found %s tree-related applications",
        len(tree_applications),
    )

    return {

        "status":
            "SUCCESS",

        "source":
            "Leeds City Council",

        "test_mode":
            True,

        "records_downloaded":
            len(features),

        "tree_related_records_found":
            len(tree_applications),

        "tree_applications":
            tree_applications,
    }
