import os
import json
import logging

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

# Your Render application URL.
PUBLIC_APP_URL = os.getenv("PUBLIC_APP_URL")

# Resend API endpoint
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

                contractors.append(
                    {
                        "id": contractor_id,
                        "name": name,
                        "email": email,
                        "distance": 4.2,
                    }
                )

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

        contractors.append(
            {
                "id": 1,
                "name": "Test Contractor",
                "email": TEST_EMAIL,
                "distance": 4.2,
            }
        )

    return contractors


# ============================================================
# WEBHOOK IDEMPOTENCY
# ============================================================

def mark_session_processed(session_id: str) -> bool:

    """
    Returns True the first time a Stripe session is seen.

    Returns False if the same session is received again.

    This prevents duplicate unlock emails when Stripe retries
    a webhook.
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
            response.choices[0]
            .message.content
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
                                    f"{refined_lead['postcode']}"
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

                "The payment link is part of the "
                "Stripe testing workflow.\n\n"

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

            contractor_results.append(
                {
                    "contractor_id":
                        contractor["id"],

                    "status":
                        "payment_link_sent",

                    "stripe_session_created":
                        True,
                }
            )

            logger.info(
                "Test payment link sent to contractor %s",
                contractor["id"]
            )

        except Exception as exc:

            logger.exception(
                "Failed contractor test"
            )

            contractor_results.append(
                {
                    "contractor_id":
                        contractor["id"],

                    "status":
                        "failed",

                    "error":
                        str(exc),
                }
            )

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
async def stripe_webhook(
    request: Request
):

    payload = await request.body()

    signature = request.headers.get(
        "stripe-signature"
    )

    if not STRIPE_WEBHOOK_SECRET:

        logger.warning(
            "STRIPE_WEBHOOK_SECRET not configured"
        )

        raise HTTPException(
            status_code=500,
            detail="Stripe webhook secret missing"
        )

    if not signature:

        raise HTTPException(
            status_code=400,
            detail="Stripe signature missing"
        )

    # ========================================================
    # VERIFY STRIPE WEBHOOK
    # ========================================================

    try:

        event = stripe.Webhook.construct_event(
            payload,
            signature,
            STRIPE_WEBHOOK_SECRET
        )

    except ValueError:

        raise HTTPException(
            status_code=400,
            detail="Invalid webhook payload"
        )

    except stripe.StripeError:

        raise HTTPException(
            status_code=400,
            detail="Invalid Stripe webhook signature"
        )

    # ========================================================
    # IGNORE EVENTS WE DON'T NEED
    # ========================================================

    if event["type"] != "checkout.session.completed":

        return {
            "status": "Event ignored"
        }

    # ========================================================
    # GET STRIPE CHECKOUT SESSION
    # ========================================================

    session = event["data"]["object"]

    # IMPORTANT:
    # Stripe gives us a Stripe object here, not a normal
    # Python dictionary. Therefore we use .id and .metadata
    # instead of .get().
    
    session_id = session.id

    metadata = session.metadata or {}

    # ========================================================
    # PREVENT DUPLICATE EMAILS
    # ========================================================

    if not mark_session_processed(
        session_id
    ):

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

    # ========================================================
    # READ LEAD DATA
    # ========================================================

    surgeon_id = metadata.get(
        "surgeon_id"
    )

    postcode = metadata.get(
        "postcode",
        "Unknown"
    )

    site_address = metadata.get(
        "site_address",
        "Unknown"
    )

    applicant_name = metadata.get(
        "applicant_name",
        "Unknown"
    )

    scope_summary = metadata.get(
        "scope_summary",
        "Unknown"
    )

    logger.info(
        "TEST PAYMENT COMPLETED for surgeon %s",
        surgeon_id
    )

    # ========================================================
    # TEST EMAIL RECIPIENT
    # ========================================================

    recipient = TEST_EMAIL

    # ========================================================
    # BUILD UNLOCK EMAIL
    # ========================================================

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

    # ========================================================
    # SEND UNLOCK EMAIL
    # ========================================================

    try:

        send_test_email(

            recipient=recipient,

            subject=(
                f"[TEST UNLOCKED] Tree Lead - "
                f"{postcode}"
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

    # ========================================================
    # SUCCESS
    # ========================================================

    return {

        "status":
            "TEST PAYMENT PROCESSED",

        "surgeon_id":
            surgeon_id,

        "recipient":
            recipient,

        "postcode":
            postcode,
    }
