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

    url = (
        "https://mapservices.leeds.gov.uk/"
        "arcgis/rest/services/Public/Planning/"
        "MapServer/12/query"
    )


    # --------------------------------------------------------
    # STEP 1:
    # Get a larger sample from the real Leeds dataset.
    #
    # We deliberately do NOT create Stripe sessions here.
    # We deliberately do NOT send emails here.
    # --------------------------------------------------------

    params = {

        "where": "1=1",

        "outFields": "*",

        "returnGeometry": "false",

        "resultRecordCount": 1000,

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
            "Failed connecting to Leeds Council API"
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Could not connect to Leeds Council API: "
                f"{str(exc)}"
            )
        )


    # --------------------------------------------------------
    # STEP 2:
    # Check for an ArcGIS error.
    # --------------------------------------------------------

    if "error" in data:

        logger.error(
            "Leeds API returned an error: %s",
            data["error"]
        )

        raise HTTPException(
            status_code=500,
            detail=f"Leeds API error: {data['error']}"
        )


    features = data.get(
        "features",
        []
    )


    logger.info(
        "Leeds returned %s records",
        len(features)
    )


    # --------------------------------------------------------
    # STEP 3:
    # Convert ArcGIS records into ordinary dictionaries.
    # --------------------------------------------------------

    applications = []

    for feature in features:

        attributes = feature.get(
            "attributes",
            {}
        )

        applications.append(
            attributes
        )


    # --------------------------------------------------------
    # STEP 4:
    # Work out the current date.
    #
    # We use UTC and look for records from approximately the
    # last 3 years as an initial test.
    #
    # This is intentionally broad for the first real test.
    # --------------------------------------------------------

    now = datetime.now(timezone.utc)

    cutoff_timestamp = (
        now.timestamp() - (3 * 365 * 24 * 60 * 60)
    ) * 1000


    # --------------------------------------------------------
    # STEP 5:
    # Keep records with a reasonably recent decision/
    # approval date.
    #
    # Leeds supplies these values as milliseconds since
    # 1 January 1970.
    # --------------------------------------------------------

    recent_applications = []

    for application in applications:

        date_deciss = application.get(
            "DATEDECISS"
        )

        date_appval = application.get(
            "DATEAPVAL"
        )

        candidate_dates = []

        if isinstance(
            date_deciss,
            (int, float)
        ):
            candidate_dates.append(
                date_deciss
            )

        if isinstance(
            date_appval,
            (int, float)
        ):
            candidate_dates.append(
                date_appval
            )

        is_recent = any(
            date_value >= cutoff_timestamp
            for date_value in candidate_dates
        )

        if is_recent:

            recent_applications.append(
                application
            )


    # --------------------------------------------------------
    # STEP 6:
    # Look for tree-related wording.
    #
    # This is NOT the final AI classification system.
    # It is simply a transparent first filter so we can
    # prove that the council data contains relevant jobs.
    # --------------------------------------------------------

    tree_keywords = [

        "tree",
        "trees",
        "tpo",
        "tree preservation",
        "felling",
        "fell",
        "arbor",
        "arboricultural",
        "pruning",
        "crown",
        "woodland",
        "hedge",
    ]


    tree_applications = []


    for application in recent_applications:

        proposal = str(
            application.get(
                "PROPOSAL",
                ""
            )
        ).lower()

        address = str(
            application.get(
                "ADDRESS",
                ""
            )
        ).lower()

        combined_text = (
            proposal
            + " "
            + address
        )


        matched_keywords = [

            keyword

            for keyword in tree_keywords

            if keyword in combined_text
        ]


        if matched_keywords:

            result = dict(
                application
            )

            result["matched_keywords"] = (
                matched_keywords
            )

            tree_applications.append(
                result
            )


    # --------------------------------------------------------
    # STEP 7:
    # Sort newest-looking records first.
    # --------------------------------------------------------

    tree_applications.sort(

        key=lambda item: max(

            [

                value

                for value in [

                    item.get("DATEDECISS"),
                    item.get("DATEAPVAL"),

                ]

                if isinstance(
                    value,
                    (int, float)
                )

            ]

            or [0]

        ),

        reverse=True,
    )


    # --------------------------------------------------------
    # STEP 8:
    # Convert timestamps into readable dates.
    # --------------------------------------------------------

    for application in tree_applications:

        for field in [
            "DATEDECISS",
            "DATEAPVAL",
        ]:

            value = application.get(
                field
            )

            if isinstance(
                value,
                (int, float)
            ):

                try:

                    application[
                        f"{field}_readable"
                    ] = datetime.fromtimestamp(
                        value / 1000,
                        tz=timezone.utc
                    ).strftime(
                        "%Y-%m-%d"
                    )

                except Exception:

                    application[
                        f"{field}_readable"
                    ] = "Unknown"


    # --------------------------------------------------------
    # STEP 9:
    # Return the test result.
    #
    # NOTHING BELOW THIS POINT creates payments or emails.
    # --------------------------------------------------------

    return {

        "status":
            "SUCCESS",

        "source":
            "Leeds City Council",

        "test_mode":
            True,

        "records_downloaded":
            len(applications),

        "recent_records_found":
            len(recent_applications),

        "tree_related_records_found":
            len(tree_applications),

        "cutoff_date":
            now.strftime("%Y-%m-%d"),

        "tree_applications":
            tree_applications[:100],
    }
