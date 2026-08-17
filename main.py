import os
import logging
import requests
import psycopg2
import urllib3
import re
import time
import math
import threading

from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# ============================================================
# VECTOR DATA LABS
# V84.0 - LEEDS LEAD FINDER
#
# PURPOSE
# ------------------------------------------------------------
# 1. Discover potential tree businesses around Leeds.
# 2. Search Leeds Council planning data.
# 3. Identify genuine tree-related applications.
# 4. Extract useful lead information.
# 5. Score and store council leads.
# 6. Run long research jobs in the background.
# ============================================================

urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)

app = FastAPI(
    title="Vector Data Labs - V84.0 Leeds Lead Finder",
    docs_url="/docs"
)

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(
    "vector-data-labs"
)


# ============================================================
# ENVIRONMENT
# ============================================================

SURL = os.getenv(
    "SUPABASE_DB_URL"
)

CH_KEY = os.getenv(
    "COMPANIES_HOUSE_KEY"
)

COMPANIES_HOUSE_URL = (
    "https://api.company-information.service.gov.uk"
)


# ============================================================
# COMPANIES HOUSE SEARCH TERMS
# ============================================================

SEARCH_TERMS = [
    "tree",
    "tree services",
    "tree surgery",
    "tree surgeon",
    "arboriculture",
    "arborist",
    "tree care",
    "tree felling",
    "stump grinding",
    "forestry",
    "landscaping"
]


ITEMS_PER_PAGE = 50

MAX_RESULTS_PER_TERM = 200

REQUEST_DELAY = 0.25


# ============================================================
# LEEDS SERVICE AREA
# ============================================================

SERVICE_RADIUS_MILES = 15.0

LEEDS_LAT = 53.8008

LEEDS_LON = -1.5491


# ============================================================
# COUNCIL SETTINGS
# ============================================================

COUNCIL_NAME = "Leeds"

COUNCIL_SOURCE = (
    "Leeds City Council"
)

COUNCIL_URL = (
    "https://mapservices.leeds.gov.uk/"
    "arcgis/rest/services/Public/"
    "Planning/MapServer/12/query"
)

# How far back a dated application may be considered recent.
RECENT_DAYS = 365

# Maximum number of council records requested
# in each ArcGIS page.
COUNCIL_PAGE_SIZE = 200


# ============================================================
# TREE BUSINESS CLASSIFICATION
# ============================================================

TREE_TERMS = [
    "tree",
    "trees",
    "arbor",
    "arborist",
    "arboriculture",
    "forestry",
    "woodland",
    "stump",
    "treecare",
    "tree care",
    "tree surgeon",
    "tree surgery",
    "tree services",
    "tree service",
    "tree felling",
    "tree removal"
]


# ============================================================
# COUNCIL TREE CLASSIFICATION
# ============================================================

TREE_GOLD = [
    "tree",
    "trees",
    "tpo",
    "fell",
    "felling",
    "felled",
    "remove",
    "removal",
    "arboriculture",
    "arborist",
    "arboricultural",
    "crown",
    "crowning",
    "crown reduction",
    "crown lift",
    "crown lifting",
    "pruning",
    "prune",
    "pollard",
    "pollarding",
    "stump",
    "stump removal",
    "oak",
    "ash",
    "willow",
    "cedar",
    "sycamore",
    "beech",
    "birch",
    "pine",
    "fir",
    "hedge",
    "woodland",
    "vegetation"
]


# Strong job/action terms receive extra points.
TREE_JOB_TERMS = [
    "fell",
    "felling",
    "remove",
    "removal",
    "pruning",
    "prune",
    "pollard",
    "pollarding",
    "crown reduction",
    "crown lift",
    "crown lifting",
    "tree works",
    "tree work",
    "arboricultural works",
    "arboricultural work",
    "stump removal"
]


# ============================================================
# POSSIBLE COUNCIL DESCRIPTION FIELDS
# ============================================================

DESCRIPTION_FIELDS = [
    "proposal",
    "description",
    "development_description",
    "developmentdescription",
    "nature",
    "details",
    "PROPOSAL",
    "siteAddress",
    "site_address",
    "address",
    "description_of_proposal",
    "application_description",
    "proposal_description",
    "planning_description",
    "development"
]


# ============================================================
# POSSIBLE DATE FIELDS
# ============================================================

DATE_FIELD_NAMES = [
    "date",
    "application_date",
    "date_received",
    "received_date",
    "valid_date",
    "registration_date",
    "decision_date",
    "date_registered",
    "submission_date",
    "start_date",
    "created_date",
    "created",
    "received",
    "validfrom",
    "valid_from"
]


# ============================================================
# POSSIBLE REFERENCE FIELDS
# ============================================================

REFERENCE_FIELD_NAMES = [
    "application_number",
    "application_no",
    "applicationreference",
    "application_reference",
    "reference",
    "ref",
    "planning_reference",
    "planning_application",
    "applicationid",
    "application_id",
    "case_number",
    "case_reference"
]


# ============================================================
# POSSIBLE ADDRESS FIELDS
# ============================================================

ADDRESS_FIELD_NAMES = [
    "siteAddress",
    "site_address",
    "siteaddress",
    "address",
    "property_address",
    "location",
    "site",
    "address_line",
    "full_address"
]


# ============================================================
# GLOBAL RESEARCH STATE
# ============================================================

research_lock = threading.Lock()

research_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "error": None
}


# ============================================================
# DATABASE SETUP
# ============================================================

def init_db():

    if not SURL:

        logger.error(
            "SUPABASE_DB_URL is missing."
        )

        return

    try:

        conn = psycopg2.connect(
            SURL
        )

        cur = conn.cursor()

        # ----------------------------------------------------
        # Potential customers
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS potential_partners (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                company_name TEXT,
                company_number TEXT UNIQUE,
                status TEXT,
                date_incorporated DATE,
                address TEXT,
                operational_confidence INT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                sic_codes TEXT[],
                discovery_source TEXT,
                companies_house_url TEXT,
                last_verified TIMESTAMPTZ,
                updated_at TIMESTAMPTZ
            );
        """)

        cur.execute("""
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS postcode TEXT;
        """)

        cur.execute("""
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS distance_from_leeds_miles NUMERIC;
        """)

        cur.execute("""
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS service_area TEXT;
        """)

        cur.execute("""
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS tree_related_name BOOLEAN DEFAULT FALSE;
        """)

        cur.execute("""
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS search_term TEXT;
        """)

        # ----------------------------------------------------
        # Council leads
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS council_leads (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                council TEXT,
                source TEXT,
                application_reference TEXT,
                application_date TIMESTAMPTZ,
                address TEXT,
                postcode TEXT,
                summary TEXT,
                matched_terms TEXT[],
                score INT DEFAULT 0,
                recent BOOLEAN DEFAULT FALSE,
                source_url TEXT,
                raw_record JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(council, application_reference, address)
            );
        """)

        conn.commit()

        cur.close()

        conn.close()

        logger.info(
            "Database schema verified and migrated."
        )

    except Exception as e:

        logger.error(
            f"Database migration error: {e}"
        )


init_db()


# ============================================================
# HTTP SESSION
# ============================================================

def get_ch_session():

    session = requests.Session()

    session.auth = (
        CH_KEY,
        ""
    )

    session.headers.update({
        "User-Agent":
            "VectorDataLabs/84.0"
    })

    return session


# ============================================================
# POSTCODE CLEANING
# ============================================================

def clean_postcode(value):

    if not value:

        return None

    value = str(
        value
    ).upper().strip()

    value = re.sub(
        r"[^A-Z0-9 ]",
        "",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    match = re.search(
        r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[A-Z]{2})\b",
        value
    )

    if not match:

        return None

    return (
        f"{match.group(1)}"
        f"{match.group(2)}"
    )


# ============================================================
# EXTRACT POSTCODE
# ============================================================

def extract_postcode(address):

    if not address:

        return None

    return clean_postcode(
        address
    )


# ============================================================
# POSTCODE -> COORDINATES
# ============================================================

def postcode_coordinates(
    session,
    postcode
):

    if not postcode:

        return None, None

    try:

        url = (
            "https://api.postcodes.io/postcodes/"
            + postcode
        )

        response = session.get(
            url,
            timeout=10
        )

        if response.status_code != 200:

            return None, None

        data = response.json()

        result = data.get(
            "result"
        )

        if not result:

            return None, None

        lat = result.get(
            "latitude"
        )

        lon = result.get(
            "longitude"
        )

        if lat is None or lon is None:

            return None, None

        return (
            float(lat),
            float(lon)
        )

    except Exception as e:

        logger.warning(
            f"Postcode lookup failed "
            f"for {postcode}: {e}"
        )

        return None, None


# ============================================================
# HAVERSINE
# ============================================================

def distance_miles(
    lat1,
    lon1,
    lat2,
    lon2
):

    earth_radius_km = 6371.0

    lat1 = math.radians(
        lat1
    )

    lat2 = math.radians(
        lat2
    )

    delta_lat = math.radians(
        lat2 - lat1
    )

    delta_lon = math.radians(
        lon2 - lon1
    )

    a = (
        math.sin(
            delta_lat / 2
        ) ** 2
        +
        math.cos(lat1)
        *
        math.cos(lat2)
        *
        math.sin(
            delta_lon / 2
        ) ** 2
    )

    c = 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a)
    )

    kilometres = (
        earth_radius_km * c
    )

    return kilometres * 0.621371


# ============================================================
# LEEDS POSTCODE TEST
# ============================================================

def is_leeds_core_postcode(
    postcode
):

    if not postcode:

        return False

    postcode = (
        postcode
        .upper()
        .replace(" ", "")
    )

    return bool(
        re.match(
            r"^LS\d{1,2}[A-Z]{2}$",
            postcode
        )
    )


# ============================================================
# TREE COMPANY NAME TEST
# ============================================================

def name_looks_tree_related(
    name
):

    if not name:

        return False

    name = name.lower()

    return any(
        term in name
        for term in TREE_TERMS
    )


# ============================================================
# SERVICE AREA
# ============================================================

def classify_service_area(
    distance
):

    if distance is None:

        return "Unknown"

    if distance <= 15:

        return (
            "Leeds 15 Mile Service Area"
        )

    return "Outside Service Area"


# ============================================================
# SAVE BUSINESS PARTNER
# ============================================================

def save_partner(
    cur,
    company,
    address,
    postcode,
    distance,
    service_area,
    tree_related_name,
    search_term
):

    company_number = company.get(
        "company_number"
    )

    if not company_number:

        return "invalid"

    cur.execute(
        """
        SELECT 1
        FROM potential_partners
        WHERE company_number = %s
        """,
        (
            company_number,
        )
    )

    if cur.fetchone():

        cur.execute(
            """
            UPDATE potential_partners
            SET
                status = %s,
                address = %s,
                postcode = %s,
                distance_from_leeds_miles = %s,
                service_area = %s,
                tree_related_name = %s,
                last_verified = NOW(),
                updated_at = NOW(),
                search_term = %s
            WHERE company_number = %s
            """,
            (
                company.get(
                    "company_status"
                ),
                address,
                postcode,
                distance,
                service_area,
                tree_related_name,
                search_term,
                company_number
            )
        )

        return "duplicate"

    cur.execute(
        """
        INSERT INTO potential_partners (
            company_name,
            company_number,
            status,
            date_incorporated,
            address,
            postcode,
            distance_from_leeds_miles,
            service_area,
            tree_related_name,
            operational_confidence,
            discovery_source,
            companies_house_url,
            last_verified,
            updated_at,
            search_term
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            NOW(),
            NOW(),
            %s
        )
        """,
        (
            company.get("title"),
            company_number,
            company.get(
                "company_status"
            ),
            company.get(
                "date_of_creation"
            ),
            address,
            postcode,
            distance,
            service_area,
            tree_related_name,
            50,
            "Companies House Leeds 15 Mile Discovery",
            (
                "https://find-and-update.company-"
                "information.service.gov.uk/company/"
                + company_number
            ),
            search_term
        )
    )

    return "new"


# ============================================================
# COMPANIES HOUSE DISCOVERY
# ============================================================

def discover_leeds_partners():

    if not CH_KEY:

        return {
            "status": "error",
            "message":
                "COMPANIES_HOUSE_KEY missing"
        }

    if not SURL:

        return {
            "status": "error",
            "message":
                "SUPABASE_DB_URL missing"
        }

    stats = {
        "status": "success",
        "search_terms": 0,
        "pages_scanned": 0,
        "companies_examined": 0,
        "active_companies": 0,
        "inactive_companies": 0,
        "valid_postcodes": 0,
        "leeds_core_matches": 0,
        "within_15_mile_matches": 0,
        "outside_service_area": 0,
        "tree_named_companies": 0,
        "new_partners_added": 0,
        "duplicates_skipped": 0,
        "api_errors": 0,
        "postcode_lookup_errors": 0,
        "sample_results": [],
        "sample_leeds_matches": []
    }

    session = get_ch_session()

    postcode_cache = {}

    seen_in_run = set()

    try:

        conn = psycopg2.connect(
            SURL
        )

        cur = conn.cursor()

        for search_term in SEARCH_TERMS:

            logger.info(
                f"Searching Companies House for: "
                f"{search_term}"
            )

            stats[
                "search_terms"
            ] += 1

            start_index = 0

            while (
                start_index
                <
                MAX_RESULTS_PER_TERM
            ):

                try:

                    response = session.get(
                        f"{COMPANIES_HOUSE_URL}"
                        "/search/companies",
                        params={
                            "q": search_term,
                            "items_per_page":
                                ITEMS_PER_PAGE,
                            "start_index":
                                start_index
                        },
                        timeout=20
                    )

                except Exception as e:

                    logger.error(
                        f"Companies House "
                        f"request error: {e}"
                    )

                    stats[
                        "api_errors"
                    ] += 1

                    break

                if response.status_code != 200:

                    logger.error(
                        "Companies House "
                        f"returned HTTP "
                        f"{response.status_code}"
                    )

                    stats[
                        "api_errors"
                    ] += 1

                    break

                try:

                    data = response.json()

                except Exception:

                    stats[
                        "api_errors"
                    ] += 1

                    break

                items = data.get(
                    "items",
                    []
                )

                if not items:

                    break

                stats[
                    "pages_scanned"
                ] += 1

                for company in items:

                    stats[
                        "companies_examined"
                    ] += 1

                    company_number = (
                        company.get(
                            "company_number"
                        )
                    )

                    if not company_number:

                        continue

                    if company_number in seen_in_run:

                        continue

                    seen_in_run.add(
                        company_number
                    )

                    status = company.get(
                        "company_status"
                    )

                    if status == "active":

                        stats[
                            "active_companies"
                        ] += 1

                    else:

                        stats[
                            "inactive_companies"
                        ] += 1

                    name = company.get(
                        "title",
                        ""
                    )

                    tree_related = (
                        name_looks_tree_related(
                            name
                        )
                    )

                    if tree_related:

                        stats[
                            "tree_named_companies"
                        ] += 1

                    address = company.get(
                        "address_snippet",
                        ""
                    )

                    postcode = extract_postcode(
                        address
                    )

                    if not postcode:

                        continue

                    stats[
                        "valid_postcodes"
                    ] += 1

                    if postcode in postcode_cache:

                        lat, lon = (
                            postcode_cache[
                                postcode
                            ]
                        )

                    else:

                        lat, lon = (
                            postcode_coordinates(
                                session,
                                postcode
                            )
                        )

                        postcode_cache[
                            postcode
                        ] = (
                            lat,
                            lon
                        )

                        if (
                            lat is None
                            or
                            lon is None
                        ):

                            stats[
                                "postcode_lookup_errors"
                            ] += 1

                            continue

                        time.sleep(
                            0.05
                        )

                    distance = distance_miles(
                        LEEDS_LAT,
                        LEEDS_LON,
                        lat,
                        lon
                    )

                    distance = round(
                        distance,
                        2
                    )

                    is_core = (
                        is_leeds_core_postcode(
                            postcode
                        )
                    )

                    if is_core:

                        stats[
                            "leeds_core_matches"
                        ] += 1

                    if (
                        distance
                        <=
                        SERVICE_RADIUS_MILES
                    ):

                        stats[
                            "within_15_mile_matches"
                        ] += 1

                        service_area = (
                            "Leeds Core"
                            if is_core
                            else
                            "Leeds 15 Mile "
                            "Service Area"
                        )

                        if status == "active":

                            result = save_partner(
                                cur,
                                company,
                                address,
                                postcode,
                                distance,
                                service_area,
                                tree_related,
                                search_term
                            )

                            if result == "new":

                                stats[
                                    "new_partners_added"
                                ] += 1

                            elif result == "duplicate":

                                stats[
                                    "duplicates_skipped"
                                ] += 1

                        if len(
                            stats[
                                "sample_leeds_matches"
                            ]
                        ) < 20:

                            stats[
                                "sample_leeds_matches"
                            ].append({
                                "company_name":
                                    name,
                                "company_number":
                                    company_number,
                                "status":
                                    status,
                                "address":
                                    address,
                                "postcode":
                                    postcode,
                                "distance_from_leeds_miles":
                                    distance,
                                "service_area":
                                    service_area,
                                "tree_related_name":
                                    tree_related
                            })

                    else:

                        stats[
                            "outside_service_area"
                        ] += 1

                    if len(
                        stats[
                            "sample_results"
                        ]
                    ) < 20:

                        stats[
                            "sample_results"
                        ].append({
                            "search_term":
                                search_term,
                            "company_name":
                                name,
                            "company_number":
                                company_number,
                            "status":
                                status,
                            "address":
                                address,
                            "postcode":
                                postcode,
                            "distance_from_leeds_miles":
                                distance,
                            "leeds_core":
                                is_core,
                            "within_15_miles":
                                (
                                    distance
                                    <=
                                    SERVICE_RADIUS_MILES
                                ),
                            "tree_related_name":
                                tree_related
                        })

                conn.commit()

                start_index += (
                    ITEMS_PER_PAGE
                )

                if len(items) < ITEMS_PER_PAGE:

                    break

                time.sleep(
                    REQUEST_DELAY
                )

        cur.close()

        conn.close()

        return stats

    except Exception as e:

        logger.exception(
            "Database save/discovery error"
        )

        return {
            "status": "error",
            "message": str(e),
            "stats": stats
        }


# ============================================================
# COUNCIL HTTP SESSION
# ============================================================

def get_council_session():

    session = requests.Session()

    session.headers.update({
        "User-Agent":
            "VectorDataLabs/84.0",
        "Referer":
            "https://www.leeds.gov.uk/"
    })

    return session


# ============================================================
# FETCH COUNCIL RECORDS
# ============================================================

def fetch_council():

    session = get_council_session()

    records = []

    offset = 0

    try:

        while True:

            params = {
                "where": "1=1",
                "outFields": "*",
                "resultRecordCount":
                    COUNCIL_PAGE_SIZE,
                "resultOffset":
                    offset,
                "orderByFields":
                    "OBJECTID DESC",
                "f": "json"
            }

            response = session.get(
                COUNCIL_URL,
                params=params,
                timeout=30,
                verify=False
            )

            if response.status_code != 200:

                logger.error(
                    "Leeds Council returned "
                    f"HTTP {response.status_code}"
                )

                return records, "Offline"

            data = response.json()

            features = data.get(
                "features",
                []
            )

            if not features:

                break

            page_records = [
                feature.get(
                    "attributes",
                    {}
                )
                for feature in features
            ]

            records.extend(
                page_records
            )

            exceeded = data.get(
                "exceededTransferLimit",
                False
            )

            if (
                len(features)
                <
                COUNCIL_PAGE_SIZE
            ):

                break

            if not exceeded and len(
                features
            ) < COUNCIL_PAGE_SIZE:

                break

            offset += (
                COUNCIL_PAGE_SIZE
            )

            # Safety limit.
            if offset >= 5000:

                break

            time.sleep(
                0.2
            )

        return records, "Online"

    except Exception as e:

        logger.error(
            f"Leeds Council request error: {e}"
        )

        return records, "Fault"


# ============================================================
# FIND FIELD VALUE
# ============================================================

def find_field_value(
    record,
    possible_names
):

    # Exact case-insensitive match first.
    for wanted in possible_names:

        for key, value in record.items():

            if str(key).lower() == (
                wanted.lower()
            ):

                if (
                    value is not None
                    and
                    str(value).strip()
                ):

                    return str(value).strip()

    # Fuzzy second pass.
    for key, value in record.items():

        key_lower = (
            str(key).lower()
        )

        if not (
            value is not None
            and
            str(value).strip()
        ):

            continue

        for wanted in possible_names:

            wanted_lower = (
                wanted.lower()
            )

            if (
                wanted_lower in key_lower
                or
                key_lower in wanted_lower
            ):

                return str(value).strip()

    return ""


# ============================================================
# DESCRIPTION EXTRACTION
# ============================================================

def extract_council_description(
    record
):

    # First check likely planning fields.
    value = find_field_value(
        record,
        DESCRIPTION_FIELDS
    )

    if value:

        return value

    # Fallback: collect useful text fields.
    candidates = []

    for key, value in record.items():

        if not isinstance(
            value,
            str
        ):

            continue

        text = value.strip()

        if not text:

            continue

        text_lower = text.lower()

        if any(
            term in text_lower
            for term in TREE_GOLD
        ):

            candidates.append(
                text
            )

    if candidates:

        # Prefer the longest useful
        # description.
        candidates.sort(
            key=len,
            reverse=True
        )

        return candidates[0]

    return ""


# ============================================================
# ADDRESS EXTRACTION
# ============================================================

def extract_council_address(
    record
):

    address = find_field_value(
        record,
        ADDRESS_FIELD_NAMES
    )

    if address:

        return address

    # Fallback to text containing
    # a postcode.
    for value in record.values():

        if not isinstance(
            value,
            str
        ):

            continue

        if extract_postcode(value):

            return value.strip()

    return ""


# ============================================================
# REFERENCE EXTRACTION
# ============================================================

def extract_application_reference(
    record
):

    reference = find_field_value(
        record,
        REFERENCE_FIELD_NAMES
    )

    if reference:

        return reference

    # Search keys containing
    # application/reference.
    for key, value in record.items():

        if value is None:

            continue

        key_lower = (
            str(key).lower()
        )

        if (
            "application" in key_lower
            and
            (
                "number" in key_lower
                or
                "reference" in key_lower
                or
                "ref" in key_lower
            )
        ):

            return str(value).strip()

        if (
            "reference" in key_lower
            and
            "url" not in key_lower
        ):

            return str(value).strip()

    return ""


# ============================================================
# DATE PARSING
# ============================================================

def parse_date_value(
    value
):

    if value is None:

        return None

    if isinstance(
        value,
        datetime
    ):

        if value.tzinfo is None:

            return value.replace(
                tzinfo=timezone.utc
            )

        return value

    # ArcGIS sometimes returns
    # milliseconds since epoch.
    if isinstance(
        value,
        (int, float)
    ):

        try:

            # milliseconds
            if value > 100000000000:

                return datetime.fromtimestamp(
                    value / 1000,
                    tz=timezone.utc
                )

            # seconds
            return datetime.fromtimestamp(
                value,
                tz=timezone.utc
            )

        except Exception:

            return None

    text = str(
        value
    ).strip()

    if not text:

        return None

    # ISO format.
    try:

        parsed = datetime.fromisoformat(
            text.replace(
                "Z",
                "+00:00"
            )
        )

        if parsed.tzinfo is None:

            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed

    except Exception:

        pass

    formats = [
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%Y",
        "%d-%m-%Y %H:%M:%S"
    ]

    for fmt in formats:

        try:

            return datetime.strptime(
                text,
                fmt
            ).replace(
                tzinfo=timezone.utc
            )

        except Exception:

            continue

    return None


# ============================================================
# DATE EXTRACTION
# ============================================================

def extract_council_date(
    record
):

    # Prefer likely date fields.
    for wanted in DATE_FIELD_NAMES:

        for key, value in record.items():

            if (
                str(key).lower()
                ==
                wanted.lower()
            ):

                parsed = parse_date_value(
                    value
                )

                if parsed:

                    return parsed

    # Fuzzy fallback.
    for key, value in record.items():

        key_lower = (
            str(key).lower()
        )

        if (
            "date" in key_lower
            or
            "received" in key_lower
            or
            "registered" in key_lower
        ):

            parsed = parse_date_value(
                value
            )

            if parsed:

                return parsed

    return None


# ============================================================
# TREE MATCHING
# ============================================================

def get_tree_matches(
    text
):

    if not text:

        return []

    text_lower = text.lower()

    matches = []

    for term in TREE_GOLD:

        if term.lower() in text_lower:

            matches.append(
                term
            )

    return sorted(
        set(matches)
    )


# ============================================================
# TREE LEAD SCORING
# ============================================================

def score_tree_lead(
    description,
    matched_terms,
    application_date
):

    if not description:

        return 0

    text = description.lower()

    score = 0

    # Base points for actual tree terms.
    score += min(
        len(matched_terms) * 5,
        30
    )

    # Strong job terms.
    strong_matches = []

    for term in TREE_JOB_TERMS:

        if term.lower() in text:

            strong_matches.append(
                term
            )

    score += min(
        len(
            set(strong_matches)
        ) * 10,
        40
    )

    # Explicit tree protection.
    if "tpo" in text:

        score += 15

    # Recent application bonus.
    if application_date:

        now = datetime.now(
            timezone.utc
        )

        age = (
            now -
            application_date
        ).days

        if age <= 30:

            score += 20

        elif age <= 90:

            score += 15

        elif age <= 180:

            score += 10

        elif age <= 365:

            score += 5

    return min(
        score,
        100
    )


# ============================================================
# RECENT TEST
# ============================================================

def is_recent_application(
    application_date
):

    if not application_date:

        return False

    cutoff = (
        datetime.now(
            timezone.utc
        )
        -
        timedelta(
            days=RECENT_DAYS
        )
    )

    return application_date >= cutoff


# ============================================================
# BUILD PROFESSIONAL LEAD
# ============================================================

def build_council_lead(
    record
):

    description = (
        extract_council_description(
            record
        )
    )

    if not description:

        return None

    matched_terms = get_tree_matches(
        description
    )

    if not matched_terms:

        return None

    address = (
        extract_council_address(
            record
        )
    )

    postcode = extract_postcode(
        address
    )

    reference = (
        extract_application_reference(
            record
        )
    )

    application_date = (
        extract_council_date(
            record
        )
    )

    recent = is_recent_application(
        application_date
    )

    score = score_tree_lead(
        description,
        matched_terms,
        application_date
    )

    # Reject very weak accidental matches.
    if score < 10:

        return None

    source_url = ""

    if reference:

        source_url = (
            "https://www.leeds.gov.uk/"
            "planning"
        )

    return {
        "council":
            COUNCIL_NAME,

        "source":
            COUNCIL_SOURCE,

        "application_reference":
            reference,

        "application_date":
            (
                application_date.isoformat()
                if application_date
                else None
            ),

        "address":
            address,

        "postcode":
            postcode,

        "summary":
            description,

        "matched_terms":
            matched_terms,

        "score":
            score,

        "recent":
            recent,

        "source_url":
            source_url,

        "raw_record":
            record
    }


# ============================================================
# SAVE COUNCIL LEAD
# ============================================================

def save_council_lead(
    cur,
    lead
):

    reference = (
        lead.get(
            "application_reference"
        )
        or ""
    )

    address = (
        lead.get(
            "address"
        )
        or ""
    )

    application_date = (
        parse_date_value(
            lead.get(
                "application_date"
            )
        )
    )

    cur.execute(
        """
        SELECT id
        FROM council_leads
        WHERE council = %s
        AND application_reference = %s
        AND address = %s
        LIMIT 1
        """,
        (
            lead["council"],
            reference,
            address
        )
    )

    existing = cur.fetchone()

    if existing:

        cur.execute(
            """
            UPDATE council_leads
            SET
                application_date = %s,
                postcode = %s,
                summary = %s,
                matched_terms = %s,
                score = %s,
                recent = %s,
                source_url = %s,
                raw_record = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (
                application_date,
                lead.get("postcode"),
                lead.get("summary"),
                lead.get("matched_terms"),
                lead.get("score"),
                lead.get("recent"),
                lead.get("source_url"),
                psycopg2.extras.Json(
                    lead.get(
                        "raw_record",
                        {}
                    )
                ),
                existing[0]
            )
        )

        return "updated"

    cur.execute(
        """
        INSERT INTO council_leads (
            council,
            source,
            application_reference,
            application_date,
            address,
            postcode,
            summary,
            matched_terms,
            score,
            recent,
            source_url,
            raw_record,
            created_at,
            updated_at
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            NOW(),
            NOW()
        )
        """,
        (
            lead["council"],
            lead["source"],
            reference,
            application_date,
            address,
            lead.get("postcode"),
            lead.get("summary"),
            lead.get("matched_terms"),
            lead.get("score"),
            lead.get("recent"),
            lead.get("source_url"),
            psycopg2.extras.Json(
                lead.get(
                    "raw_record",
                    {}
                )
            )
        )
    )

    return "new"


# ============================================================
# COUNCIL LEAD DISCOVERY
# ============================================================

def discover_council_leads():

    stats = {
        "status": "success",
        "records_downloaded": 0,
        "tree_related_records": 0,
        "recent_tree_leads": 0,
        "old_tree_records": 0,
        "records_without_dates": 0,
        "new_leads": 0,
        "updated_leads": 0,
        "strong_leads": 0,
        "leads": []
    }

    records, council_status = (
        fetch_council()
    )

    stats[
        "council_status"
    ] = council_status

    stats[
        "records_downloaded"
    ] = len(records)

    if not SURL:

        stats[
            "status"
        ] = "error"

        stats[
            "error"
        ] = "SUPABASE_DB_URL missing"

        return stats

    try:

        conn = psycopg2.connect(
            SURL
        )

        cur = conn.cursor()

        discovered = []

        for record in records:

            lead = build_council_lead(
                record
            )

            if not lead:

                continue

            stats[
                "tree_related_records"
            ] += 1

            if lead["recent"]:

                stats[
                    "recent_tree_leads"
                ] += 1

            else:

                application_date = (
                    lead.get(
                        "application_date"
                    )
                )

                if application_date:

                    stats[
                        "old_tree_records"
                    ] += 1

            if not lead.get(
                "application_date"
            ):

                stats[
                    "records_without_dates"
                ] += 1

            if lead["score"] >= 50:

                stats[
                    "strong_leads"
                ] += 1

            result = save_council_lead(
                cur,
                lead
            )

            if result == "new":

                stats[
                    "new_leads"
                ] += 1

            elif result == "updated":

                stats[
                    "updated_leads"
                ] += 1

            discovered.append(
                lead
            )

        conn.commit()

        cur.close()

        conn.close()

        # Highest-quality and most recent first.
        discovered.sort(
            key=lambda x: (
                x.get("recent", False),
                x.get("score", 0),
                x.get(
                    "application_date"
                ) or ""
            ),
            reverse=True
        )

        # Return only a manageable sample
        # to the browser.
        stats[
            "leads"
        ] = discovered[:50]

        return stats

    except Exception as e:

        logger.exception(
            "Council lead database error"
        )

        stats[
            "status"
        ] = "error"

        stats[
            "error"
        ] = str(e)

        return stats


# ============================================================
# COMPLETE BACKGROUND RESEARCH
# ============================================================

def perform_full_research():

    logger.info(
        "BACKGROUND RESEARCH STARTED"
    )

    try:

        business_result = (
            discover_leeds_partners()
        )

        council_result = (
            discover_council_leads()
        )

        result = {
            "business_discovery":
                business_result,

            "council_lead_discovery":
                council_result
        }

        with research_lock:

            research_state[
                "running"
            ] = False

            research_state[
                "finished_at"
            ] = datetime.now(
                timezone.utc
            ).isoformat()

            research_state[
                "last_result"
            ] = result

            research_state[
                "error"
            ] = None

        logger.info(
            "BACKGROUND RESEARCH FINISHED"
        )

    except Exception as e:

        logger.exception(
            "BACKGROUND RESEARCH FAILED"
        )

        with research_lock:

            research_state[
                "running"
            ] = False

            research_state[
                "finished_at"
            ] = datetime.now(
                timezone.utc
            ).isoformat()

            research_state[
                "error"
            ] = str(e)


# ============================================================
# START BACKGROUND RESEARCH
# ============================================================

def start_research():

    with research_lock:

        if research_state[
            "running"
        ]:

            return False

        research_state[
            "running"
        ] = True

        research_state[
            "started_at"
        ] = datetime.now(
            timezone.utc
        ).isoformat()

        research_state[
            "finished_at"
        ] = None

        research_state[
            "last_result"
        ] = None

        research_state[
            "error"
        ] = None

    worker = threading.Thread(
        target=perform_full_research,
        daemon=True
    )

    worker.start()

    return True


# ============================================================
# HOME PAGE
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def lander():

    return """
    <html>

    <head>

        <title>
            Vector Data Labs
        </title>

        <meta
            name="viewport"
            content="width=device-width,
            initial-scale=1"
        >

    </head>

    <body style="
        font-family:Arial,sans-serif;
        text-align:center;
        padding-top:50px;
        background:#f4f4f9;
    ">

        <div style="
            display:inline-block;
            padding:45px;
            background:white;
            border-radius:15px;
            box-shadow:
                0 10px 30px
                rgba(0,0,0,0.1);
            border-top:6px solid #1b5e20;
            max-width:700px;
        ">

            <h1 style="
                color:#1b5e20;
            ">
                Vector Data Labs
            </h1>

            <h2>
                V84.0 Leeds Lead Finder
            </h2>

            <div style="
                background:#f1f8e9;
                padding:15px;
                border-radius:8px;
                margin:20px 0;
                text-align:left;
                font-size:14px;
                line-height:1.7;
            ">

                <b>Business discovery:</b>
                Companies House<br>

                <b>Business area:</b>
                Leeds + 15 miles<br>

                <b>Lead source:</b>
                Leeds City Council<br>

                <b>Lead type:</b>
                Tree-related planning applications<br>

                <b>Lead storage:</b>
                Supabase

            </div>

            <a href="/research-leeds"
               style="
                display:inline-block;
                padding:12px 25px;
                background:#1b5e20;
                color:white;
                text-decoration:none;
                border-radius:5px;
                font-weight:bold;
            ">
                Run Leeds Research
            </a>

            <br><br>

            <a href="/research-status"
               style="color:#555;">
                Check Research Status
            </a>

            <br><br>

            <a href="/council-leads"
               style="color:#555;">
                View Stored Council Leads
            </a>

            <br><br>

            <a href="/test-regional"
               style="color:#555;">
                Run Council Test
            </a>

            <br><br>

            <a href="/docs"
               style="color:#555;">
                API Documentation
            </a>

        </div>

    </body>

    </html>
    """


# ============================================================
# START FULL RESEARCH
# ============================================================

@app.get(
    "/research-leeds"
)
def run_discovery():

    started = start_research()

    if not started:

        return {
            "status":
                "already_running",

            "message":
                "Leeds business and council "
                "research is already running "
                "in the background.",

            "check":
                "/research-status"
        }

    return {
        "status":
            "started",

        "message":
            "Leeds business and council "
            "research has started in "
            "the background.",

        "check":
            "/research-status"
    }


# ============================================================
# RESEARCH STATUS
# ============================================================

@app.get(
    "/research-status"
)
def research_status():

    with research_lock:

        return {
            "status":
                "running"
                if research_state[
                    "running"
                ]
                else
                "finished",

            "running":
                research_state[
                    "running"
                ],

            "started_at":
                research_state[
                    "started_at"
                ],

            "finished_at":
                research_state[
                    "finished_at"
                ],

            "error":
                research_state[
                    "error"
                ],

            "last_result":
                research_state[
                    "last_result"
                ]
        }


# ============================================================
# COUNCIL TEST
# ============================================================

@app.get(
    "/test-regional"
)
def test_leeds():

    result = discover_council_leads()

    return {
        "council":
            "Leeds",

        "source":
            "Leeds City Council",

        "records_checked":
            result.get(
                "records_downloaded",
                0
            ),

        "tree_related_records":
            result.get(
                "tree_related_records",
                0
            ),

        "recent_tree_leads":
            result.get(
                "recent_tree_leads",
                0
            ),

        "strong_leads":
            result.get(
                "strong_leads",
                0
            ),

        "new_leads":
            result.get(
                "new_leads",
                0
            ),

        "updated_leads":
            result.get(
                "updated_leads",
                0
            ),

        "leads":
            result.get(
                "leads",
                []
            )
    }


# ============================================================
# VIEW STORED COUNCIL LEADS
# ============================================================

@app.get(
    "/council-leads"
)
def council_leads():

    if not SURL:

        return {
            "status":
                "error",

            "message":
                "SUPABASE_DB_URL missing"
        }

    try:

        conn = psycopg2.connect(
            SURL
        )

        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                council,
                source,
                application_reference,
                application_date,
                address,
                postcode,
                summary,
                matched_terms,
                score,
                recent,
                source_url,
                created_at,
                updated_at
            FROM council_leads
            ORDER BY
                recent DESC,
                score DESC,
                application_date DESC NULLS LAST
            LIMIT 100
            """
        )

        rows = cur.fetchall()

        cur.close()

        conn.close()

        leads = []

        for row in rows:

            leads.append({
                "council":
                    row[0],

                "source":
                    row[1],

                "application_reference":
                    row[2],

                "application_date":
                    (
                        row[3].isoformat()
                        if row[3]
                        else None
                    ),

                "address":
                    row[4],

                "postcode":
                    row[5],

                "summary":
                    row[6],

                "matched_terms":
                    row[7],

                "score":
                    row[8],

                "recent":
                    row[9],

                "source_url":
                    row[10],

                "created_at":
                    (
                        row[11].isoformat()
                        if row[11]
                        else None
                    ),

                "updated_at":
                    (
                        row[12].isoformat()
                        if row[12]
                        else None
                    )
            })

        return {
            "status":
                "success",

            "count":
                len(leads),

            "leads":
                leads
        }

    except Exception as e:

        logger.exception(
            "Unable to retrieve council leads"
        )

        return {
            "status":
                "error",

            "message":
                str(e)
        }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get(
    "/health"
)
def health():

    return {
        "status":
            "online",

        "version":
            "84.0",

        "database_configured":
            bool(SURL),

        "companies_house_configured":
            bool(CH_KEY),

        "service_area_miles":
            SERVICE_RADIUS_MILES,

        "council":
            COUNCIL_NAME,

        "recent_lead_window_days":
            RECENT_DAYS,

        "research_running":
            research_state[
                "running"
            ]
    }


# ============================================================
# RUN LOCALLY
# ============================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
