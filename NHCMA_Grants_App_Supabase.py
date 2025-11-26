# --- Imports ---
import os, json, smtplib, io, hashlib, pathlib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, Tuple, Optional
from urllib.parse import urlparse, parse_qs
from urllib.parse import quote
import pandas as pd
import streamlit as st
from supabase import create_client, Client
import bcrypt
import secrets
from itsdangerous import URLSafeTimedSerializer
from streamlit_cookies_manager import EncryptedCookieManager

APP_TITLE = "NHCMA Foundation — 2025 Public Health Innovation Grants"
TIMEZONE = "America/New_York"

# ========= Admin-configurable Deadlines =========
SETTINGS_KEYS = {
    "org": "org_deadline_iso",
    "stu": "stu_deadline_iso",
}

def _parse_iso_to_et(iso_s: str, fallback_dt: datetime) -> datetime:
    try:
        # Accept 'Z' or explicit offsets
        dt = datetime.fromisoformat(iso_s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo(TIMEZONE))
    except Exception:
        return fallback_dt

def get_deadlines(sb_read) -> tuple[datetime, datetime]:
    """Return (ORG_DEADLINE_ET, STU_DEADLINE_ET) with DB override if present."""
    # hardcoded fallback (kept!)
    org_fb = datetime(2025, 12, 27, 23, 59, tzinfo=ZoneInfo(TIMEZONE))
    stu_fb = datetime(2025, 12, 27, 23, 59, tzinfo=ZoneInfo(TIMEZONE))
    try:
        rows = sb_read.table("app_settings").select("key,value").in_("key", [
            SETTINGS_KEYS["org"], SETTINGS_KEYS["stu"]
        ]).execute().data or []
        mp = {r["key"]: r["value"] for r in rows}
        org = _parse_iso_to_et(mp.get(SETTINGS_KEYS["org"], ""), org_fb)
        stu = _parse_iso_to_et(mp.get(SETTINGS_KEYS["stu"], ""), stu_fb)
        return org, stu
    except Exception:
        return org_fb, stu_fb

def set_deadline(sb_write, track: str, dt_local: datetime):
    """Persist a deadline as ISO with proper timezone offset."""
    key = SETTINGS_KEYS["org" if track == "organization" else "stu"]
    iso_val = dt_local.isoformat()
    sb_write.table("app_settings").upsert(
        {"key": key, "value": iso_val},
        on_conflict="key"
    ).execute()
# ========= /Admin-configurable Deadlines =========
# ========= President Info (for award & decline letters) =========
PRESIDENT_KEYS = {
    "name": "letter_president_name",
    "title": "letter_president_title",
    "email": "letter_president_email",
}

def get_president_settings(sb_read) -> Dict[str, str]:
    """Return president name/title/email for letters, with safe defaults."""
    defaults = {
        "name": "Steve Saunders, MD, MBA",
        "title": "President",
        "email": "nhcma@lutinemanagement.com",
    }
    try:
        rows = sb_read.table("app_settings").select("key,value") \
            .in_("key", list(PRESIDENT_KEYS.values())) \
            .execute().data or []
        mp = {r["key"]: r["value"] for r in rows}
        return {
            "name": mp.get(PRESIDENT_KEYS["name"], defaults["name"]),
            "title": mp.get(PRESIDENT_KEYS["title"], defaults["title"]),
            "email": mp.get(PRESIDENT_KEYS["email"], defaults["email"]),
        }
    except Exception:
        return defaults

def set_president_settings(sb_write, name: str, title: str, email: str) -> None:
    payload = [
        {"key": PRESIDENT_KEYS["name"], "value": (name or "").strip()},
        {"key": PRESIDENT_KEYS["title"], "value": (title or "").strip()},
        {"key": PRESIDENT_KEYS["email"], "value": (email or "").strip()},
    ]
    sb_write.table("app_settings").upsert(payload, on_conflict="key").execute()
# ========= /President Info =========

# --- Build/Version Banner (always visible, no duplicates) ---
st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="collapsed")

APP_VERSION = os.environ.get("APP_VERSION", "")        # set in Streamlit env vars (optional)
APP_COMMIT  = os.environ.get("APP_COMMIT", "")         # optional short SHA from git
SHA12 = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()[:12]

# Show on page body so it’s impossible to miss; sidebar is optional
# st.caption(f"🔧 Build: {APP_VERSION or 'local'} | SHA: {SHA12}{(' | Commit: ' + APP_COMMIT) if APP_COMMIT else ''}")
# try:
    # st.sidebar.info(f"Build: {APP_VERSION or 'local'} | SHA: {SHA12}{(' | Commit: ' + APP_COMMIT) if APP_COMMIT else ''}")
# except Exception:
    # pass
    
PROJECT_REF = "icjunpjliexaacexjgwy"
EDGE_BASE   = f"https://{PROJECT_REF}.supabase.co/functions/v1"

# ========= Grant Decision Helpers =========
# decision: "funded" or "declined"
# amount: numeric (nullable if declined)

def get_decision(sb_read, submission_id: str) -> Optional[Dict[str, Any]]:
    """Fetch decision for a submission_id, or None."""
    try:
        rows = sb_read.table("grant_decisions").select("*") \
            .eq("submission_id", submission_id).execute().data or []
        return rows[0] if rows else None
    except Exception:
        return None

def set_decision(sb_write, submission_id: str, decision: str, amount: Optional[float]):
    """Insert/update decision row."""

    # Normalize input (recommended but optional)
    decision = (decision or "").lower().strip()
    if decision not in ("funded", "declined"):
        raise ValueError(f"Invalid decision: {decision}")

    payload = {
        "submission_id": submission_id,
        "decision": decision,        # normalized: "funded" or "declined"
        "amount_awarded": amount,    # None if declined
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

    sb_write.table("grant_decisions").upsert(payload, on_conflict="submission_id").execute()

def get_all_decisions(sb_read) -> Dict[str, Dict[str, Any]]:
    """Return mapping submission_id → decision row."""
    try:
        rows = sb_read.table("grant_decisions").select("*").execute().data or []
        return {r["submission_id"]: r for r in rows}
    except Exception:
        return {}
# ========= /Grant Decision Helpers =========

def edge_signed_download_url(bucket: str, path: str) -> str:
    return f"{EDGE_BASE}/signed-download?bucket={quote(bucket)}&path={quote(path, safe='/')}"

def _bucket_and_key_from_url(u: str) -> tuple[str, str]:
    if isinstance(u, str) and u.startswith("http"):
        m = "/storage/v1/object/public/"
        if m in u:
            rest = u.split(m, 1)[1].split("?", 1)[0]
            bucket, _, key = rest.partition("/")
            return bucket, key
        m = "/storage/v1/object/sign/"
        if m in u:
            rest = u.split(m, 1)[1].split("?", 1)[0]
            bucket, _, key = rest.partition("/")
            return bucket, key
    # use configured bucket for bare keys
    return BUCKET or "nhcma-uploads", (u or "").lstrip("/")


def to_edge(u: str) -> str:
    if not isinstance(u, str) or not u.strip():
        return ""
    b, k = _bucket_and_key_from_url(u)
    return edge_signed_download_url(b, k)    

def make_excel(df: pd.DataFrame) -> bytes:
    """Return an .xlsx bytes blob for Streamlit download_button."""
    with io.BytesIO() as output:
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Submissions")
        return output.getvalue()

def submission_notice():
    st.info(
        "📝 Please have all documentation ready before you begin. "
        "You must complete and submit the application in one session; "
        "if you leave before submitting, you will need to start over."
    )

# Deadlines (ET)
ORG_DEADLINE = datetime(2025, 12, 27, 23, 59, tzinfo=ZoneInfo(TIMEZONE))
STU_DEADLINE = datetime(2025, 12, 27, 23, 59, tzinfo=ZoneInfo(TIMEZONE))

# Supabase config
_sb = st.secrets.get("supabase", {})
SUPABASE_URL     = os.getenv("SUPABASE_URL")     or st.secrets.get("SUPABASE_URL")     or _sb.get("url")
SUPABASE_ANON_KEY= os.getenv("SUPABASE_ANON_KEY")or st.secrets.get("SUPABASE_ANON_KEY")or _sb.get("anon_key")
BUCKET           = os.getenv("SUPABASE_BUCKET")  or st.secrets.get("SUPABASE_BUCKET")  or _sb.get("bucket", "nhcma-uploads")

# Create anon client
sb: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


# Optional: create service-role client for bypassing RLS (server-side only)
SERVICE_ROLE_KEY = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")
sb_admin = create_client(SUPABASE_URL, SERVICE_ROLE_KEY) if SERVICE_ROLE_KEY else None

@st.cache_resource(show_spinner=False)
def supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        st.error("Missing SUPABASE_URL or SUPABASE_ANON_KEY in secrets.")
        st.stop()
    return create_client(str(SUPABASE_URL), str(SUPABASE_ANON_KEY))

sb = supabase_client()

# ========= Judge PIN + Cookie Sessions (lazy init) =========
SESSION_TTL_DAYS = int(st.secrets.get("SESSION_TTL_DAYS", "30"))
BCRYPT_ROUNDS    = int(st.secrets.get("BCRYPT_ROUNDS", "12"))
COOKIE_SIGNING_KEY = st.secrets["COOKIE_SIGNING_KEY"]

# Lazy cookie/signing init so submissions aren’t affected
cookies = None
signer = None

def _ensure_cookie_env():
    """Initialize cookies + signer only when judging auth is in play."""
    global cookies, signer
    if cookies is not None and signer is not None:
        return
    # Import here to avoid global hard dependency during submission-only runs
    from streamlit_cookies_manager import EncryptedCookieManager
    from itsdangerous import URLSafeTimedSerializer

    c = EncryptedCookieManager(prefix="nhcma_", password=COOKIE_SIGNING_KEY)
    if not c.ready():
        st.stop()
    s = URLSafeTimedSerializer(COOKIE_SIGNING_KEY)
    cookies, signer = c, s

def hash_pin(pin: str) -> str:
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    return bcrypt.hashpw(pin.encode("utf-8"), salt).decode("utf-8")

def verify_pin(pin: str, pin_hash: str) -> bool:
    try:
        return bcrypt.checkpw(pin.encode("utf-8"), pin_hash.encode("utf-8"))
    except Exception:
        return False

def _require_admin():
    if not sb_admin:
        st.error("Server-side credentials missing (SUPABASE_SERVICE_ROLE_KEY). Judging auth requires service role.")
        st.stop()

def _db_set_pin(judge_id: str, pin_hash: str):
    _require_admin()
    sb_admin.table("judges").update({
        "pin_hash": pin_hash,
        "pin_set_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", judge_id).execute()

def _db_judge_by_email(email: str):
    _require_admin()
    rows = sb_admin.table("judges").select("*").eq("email", email.lower().strip()).limit(1).execute().data or []
    return rows[0] if rows else None

def _db_judge_by_id(judge_id: str):
    _require_admin()
    rows = sb_admin.table("judges").select("*").eq("id", judge_id).limit(1).execute().data or []
    return rows[0] if rows else None

def _db_create_session(judge_id: str) -> str:
    _require_admin()
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    sb_admin.table("judge_sessions").insert({
        "judge_id": judge_id,
        "session_token": token,
        "expires_at": expires.isoformat()
    }).execute()
    return token

def _db_validate_session(token: str):
    _require_admin()
    if not token:
        return None
    rows = (sb_admin.table("judge_sessions")
            .select("*, judges:judge_id(full_name,email)")
            .eq("session_token", token)
            .limit(1).execute().data or [])
    if not rows:
        return None
    row = rows[0]
    exp = datetime.fromisoformat(row["expires_at"].replace("Z","")).astimezone(timezone.utc)
    if exp < datetime.now(timezone.utc):
        return None
    return {
        "judge_id": row["judge_id"],
        "name": row["judges"]["full_name"],
        "email": row["judges"]["email"],
    }

def set_cookie_session(token: str):
    _ensure_cookie_env()
    signed = signer.dumps({"t": token})
    # NEW API: dict-style assignment + save()
    cookies["nhcma_judge"] = signed
    cookies.save()

def get_cookie_session():
    _ensure_cookie_env()
    # NEW API: dict-style get()
    raw = cookies.get("nhcma_judge")
    if not raw:
        return None
    try:
        payload = signer.loads(raw, max_age=SESSION_TTL_DAYS * 24 * 3600)
        return payload.get("t")
    except Exception:
        return None

def clear_cookie_session():
    _ensure_cookie_env()
    # NEW API: delete key then save()
    if "nhcma_judge" in cookies:
        del cookies["nhcma_judge"]
        cookies.save()

    # ========= /Judge PIN + Cookie Sessions =========

# SMTP config (supports flat keys or [smtp] section)
_smtp = st.secrets.get("smtp", {})
SMTP_HOST = os.getenv("SMTP_HOST") or st.secrets.get("SMTP_HOST") or _smtp.get("host", "smtp.office365.com")
SMTP_PORT = int(os.getenv("SMTP_PORT") or st.secrets.get("SMTP_PORT") or _smtp.get("port", 587))
SMTP_USER = os.getenv("SMTP_USER") or st.secrets.get("SMTP_USER") or _smtp.get("user")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD") or st.secrets.get("SMTP_PASSWORD") or _smtp.get("password")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL") or st.secrets.get("SMTP_FROM_EMAIL") or _smtp.get("from_addr") or SMTP_USER
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME") or st.secrets.get("SMTP_FROM_NAME") or _smtp.get("from_name", "NHCMA Foundation Grants")
CC_EMAIL = "nhcma@lutinemanagement.com"


def too_late(deadline: datetime) -> bool:
    now = datetime.now(ZoneInfo(TIMEZONE))
    return now > deadline

def save_upload_to_storage(file, prefix: str) -> str:
    if file is None:
        return ""
    safe_name = file.name.replace("/", "_").replace("\\", "_")
    key = f"{prefix}/{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{safe_name}"

    # Read bytes from Streamlit UploadedFile
    try:
        file_bytes = file.getvalue()          # bytes, not UploadedFile
    except Exception:
        try:
            file_bytes = file.read()          # fallback
        except Exception:
            file_bytes = None
    if not file_bytes:
        st.warning(f"Upload failed for {safe_name}: could not read file bytes")
        return ""

    # Build file_options with **string** header values
    content_type = getattr(file, "type", None) or "application/octet-stream"
    file_options = {
        "content-type": str(content_type),  # must be str
        "upsert": "true",                   # must be str; bool causes header error
        # "cache-control": "3600",          # optional, must be str if you add it
    }

    # Upload BYTES to Supabase Storage
    try:
        sb.storage.from_(BUCKET).upload(key, file_bytes, file_options=file_options)
    except Exception as e:
        st.warning(f"Upload failed for {safe_name}: {e}")
        return ""

    # Return a signed URL (fallback to public URL)
    try:
        signed = sb.storage.from_(BUCKET).create_signed_url(key, expires_in=60*60*24*7)
        if isinstance(signed, dict):
            return signed.get("signedURL") or signed.get("signed_url") or ""
        return str(signed)
    except Exception as e:
        st.warning(f"Could not create signed URL for {safe_name}: {e}")
        try:
            return sb.storage.from_(BUCKET).get_public_url(key)
        except Exception:
            return ""

def insert_submission(track: str, applicant_name: str, email: str, phone: str, payload: Dict[str, Any], uploads: Dict[str, str]) -> Optional[int]:
    data = {
        "track": track,
        "applicant_name": (applicant_name or "").strip(),
        "email": (email or "").strip(),
        "phone": (phone or "").strip(),
        "payload_json": payload,
        "uploads_json": uploads,
    }

    try:
        # use service-role client if available, otherwise fall back to anon
        client = sb_admin or sb
        res = client.table("submissions").insert(data).execute()
        if getattr(res, "data", None):
            return res.data[0].get("id")
    except Exception as e:
        st.error(f"Error saving submission: {e}")
    return None

def load_submissions_df() -> pd.DataFrame:
    try:
        client = sb_admin or sb
        res = client.table("submissions").select("*").order("id", desc=True).execute()
        rows = res.data or []
    except Exception as e:
        st.error(f"Error loading submissions: {e}")
        rows = []

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Expand payload_json into columns dynamically
    payloads = pd.json_normalize(df["payload_json"])
    payloads = payloads.add_prefix("Q: ")   # optional prefix for clarity

    # Expand uploads_json into columns
    uploads = pd.json_normalize(df["uploads_json"])
    uploads = uploads.rename(columns={
        "proposal": "Proposal URL",
        "budget": "Budget URL",
        "other": "Other URL"
    })

    # Concatenate back together
    df = pd.concat([df.drop(["payload_json","uploads_json"], axis=1), payloads, uploads], axis=1)
    return df

# ----------------------------
# Email
# ----------------------------
def send_email(to_email: str, cc_email: Optional[str], subject: str, html_body: str) -> bool:
    """Send email via Office365 SMTP using secrets. Returns True on success."""
    if not (SMTP_USER and SMTP_PASSWORD and SMTP_FROM_EMAIL):
        st.warning("Email not sent: SMTP credentials are missing in secrets.")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    msg["To"] = to_email
    if cc_email:
        msg["Cc"] = cc_email
    msg.set_content("This email requires an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        st.warning(f"Email send failed: {e}")
        return False

def build_confirmation_email(track: str, payload: Dict[str, Any], record_id: Optional[int]) -> str:
    ts = datetime.now(ZoneInfo(TIMEZONE)).strftime("%b %d, %Y %I:%M %p %Z")
    name = payload.get("applicant_name") or payload.get("org_name") or ""
    title = payload.get("project_title","")
    org = payload.get("org_name","") if track=="organization" else ""
    school = payload.get("school","") if track=="student" else ""
    lines = [
        f"<p>Dear {payload.get('applicant_name','Applicant')},</p>",
        "<p>Thank you for your submission to the <strong>NHCMA Foundation — 2025 Public Health Innovation Grants</strong>.</p>",
        f"<p><strong>Track:</strong> {track.title()}<br>",
        f"<strong>Project Title:</strong> {title or '—'}<br>",
        f"{'<strong>Organization:</strong> '+org+'<br>' if org else ''}",
        f"{'<strong>School:</strong> '+school+'<br>' if school else ''}",
        f"<strong>Timestamp:</strong> {ts}<br>",
        f"<strong>Submission ID:</strong> {record_id or '—'}</p>",
        "<p>We will contact you if additional information is needed. Questions may be directed to <a href='mailto:nhcma@lutinemanagement.com'>nhcma@lutinemanagement.com</a>.</p>",
        "<p>— NHCMA Foundation</p>"
    ]
    return "\n".join(lines)

# ========= Award / Decline Letter Builders =========

def build_award_letter_html(sub: Dict[str, Any], pres: Dict[str, str], amount: float) -> str:
    """Builds HTML for a funded grant decision using normalized keys."""

    applicant = sub.get("applicant_name") or "Applicant"
    project = sub.get("project_title") or "your project"
    category = sub.get("applicant_category") or ""
    program = sub.get("program") or ""
    award_amt = f"${amount:,.2f}"

    # NOTE: use 'name' and 'title' which is what get_president_settings returns
    pres_name = pres.get("name", "NHCMA Foundation President")
    pres_title = pres.get("title", "President")

    lines = [
        f"<p>Dear {applicant},</p>",

        (
            "<p>Congratulations on being awarded a grant from the "
            "<strong>New Haven County Medical Association Foundation</strong> "
            f"for the program titled “{project}{f', {program}' if program else ''}.” "
            f"This grant, in the category of “{category}”, has been awarded to support "
            "your efforts and contributions in this field.</p>"
        ),

        f"<p><strong>The awarded grant amount is {award_amt}.</strong></p>",

        (
            "<p>As a grant recipient, we kindly ask you to plan to attend the "
            "NHCMA Annual Meeting in the 4th quarter of next year, where you will have the opportunity "
            "to present the results of your funded project. More details will be sent as the date is finalized.</p>"
        ),

        (
            "<p>Please note that checks will be mailed within the next week. "
            "To ensure we have the correct mailing address, kindly email your preferred address to the "
            "NHCMA staff at <a href='mailto:NHCMA@lutinemanagement.com'>NHCMA@lutinemanagement.com</a>.</p>"
        ),

        "<p>Please feel free to reach out for any further assistance or clarification. "
        "We look forward to seeing the impactful results of your work.</p>",

        f"<p>Sincerely,<br>{pres_name}<br>{pres_title}<br>NHCMA Foundation</p>"
    ]

    return "\n".join(lines)


def build_decline_letter_html(sub: Dict[str, Any], pres: Dict[str, str]) -> str:
    """Builds HTML for a declined grant decision using normalized keys."""

    applicant = sub.get("applicant_name") or "Applicant"
    project = sub.get("project_title") or "your project"
    category = sub.get("applicant_category") or ""

    # NOTE: same key fix here
    pres_name = pres.get("name", "NHCMA Foundation President")
    pres_title = pres.get("title", "President")

    lines = [
        f"<p>Dear {applicant},</p>",

        (
            "<p>Thank you for submitting your proposal to the "
            "<strong>NHCMA Foundation — 2025 Public Health Innovation Grants</strong>. "
            "This year we received a large number of thoughtful and high-quality applications.</p>"
        ),

        (
            "<p>After a careful and competitive review process, we regret to inform you that your proposal "
            "was not selected for funding. Unfortunately, we are not able to fund all worthy projects, "
            "and many strong applications could not be supported this cycle.</p>"
        ),

        (
            f"<p>Your proposal, titled “{project}”, reflected meaningful work, "
            "and we strongly encourage you to consider reapplying in a future cycle.</p>"
        ),

        "<p>We appreciate your commitment to advancing public health in our community.</p>",

        f"<p>Sincerely,<br>{pres_name}<br>{pres_title}<br>NHCMA Foundation</p>"
    ]

    return "\n".join(lines)

def html_to_pdf_bytes(html: str) -> bytes:
    """
    Lightweight fallback: generate a PDF from HTML using ReportLab.
    This avoids depending on external binaries like wkhtmltopdf.
    """
    from reportlab.platypus import SimpleDocTemplate, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            topMargin=0.75*inch,
                            bottomMargin=0.75*inch,
                            leftMargin=0.75*inch,
                            rightMargin=0.75*inch)

    styles = getSampleStyleSheet()
    story = [Paragraph(html, styles["Normal"])]

    doc.build(story)
    return buf.getvalue()

# ========= /Award / Decline Letter Builders =========


# ----------------------------
# Validation helpers
# ----------------------------
def _missing_student_fields(applicant_name, school, email, phone, project_title):
    missing = []
    if not (applicant_name or "").strip():
        missing.append("Applicant Name")
    if not (school or "").strip() or school == "— Select your school —":
        missing.append("Medical School (select an option)")
    if not (email or "").strip():
        missing.append("School Email")
    if not (phone or "").strip():
        missing.append("Phone")
    if not (project_title or "").strip():
        missing.append("Project Title")
    return missing

def _missing_org_fields(org_name, applicant_name, email, project_title):
    missing = []
    if not (org_name or "").strip():
        missing.append("Name of Organization")
    if not (applicant_name or "").strip():
        missing.append("Applicant Name")
    if not (email or "").strip():
        missing.append("Applicant Email")
    if not (project_title or "").strip():
        missing.append("Project Title")
    return missing

# ----------------------------
# Forms with unique keys
# ----------------------------
def org_form() -> Tuple[bool, Dict[str, Any], Dict[str, str], str, str, str]:
    st.subheader("Organization Application (2025)", anchor="org")
    submission_notice()
    st.caption(
    f"Submission deadline: **{ORG_DEADLINE.strftime('%B %d, %Y at %I:%M %p %Z')}**\n\n_Required fields are marked with *_."
)

    disabled = too_late(ORG_DEADLINE)
    if disabled:
        st.error("The organization submission deadline has passed.")

    org_name = st.text_input("Name of Organization*", key="org_org_name", disabled=disabled)
    applicant_name = st.text_input("Name of Applicant (First/Last)*", key="org_applicant_name", disabled=disabled)
    email = st.text_input("Applicant Email*", key="org_email", disabled=disabled)
    phone = st.text_input("Applicant Phone*", key="org_phone", disabled=disabled)

    exec_dir = st.text_input("Executive Director (First/Last)", key="org_exec_dir", disabled=disabled)
    exec_email = st.text_input("Executive Director Email", key="org_exec_email", disabled=disabled)
    exec_phone = st.text_input("Executive Director Phone", key="org_exec_phone", disabled=disabled)

    mission = st.text_area("Organization Mission (brief)", key="org_mission", disabled=disabled)

    st.markdown("**Eligibility (must confirm all):**")
    eligible_nonprofit = st.checkbox("Organization is a not-for-profit.", key="org_elig_np", disabled=disabled)
    eligible_report = st.checkbox("Recipient will present final report at the NHCMA winter meeting in 2026 (date TBA).", key="org_elig_report", disabled=disabled)
    eligible_benefit = st.checkbox("Funding will benefit residents of the Greater New Haven area.", key="org_elig_benefit", disabled=disabled)

    st.markdown("**Introduction & Purpose (≈250 words each):**")
    q1 = st.text_area("1) Public health issue addressed in Greater New Haven", key="org_q1", disabled=disabled)
    q2 = st.text_area("2) Alignment with NHCMA Foundation mission", key="org_q2", disabled=disabled)
    q3 = st.text_area("3) Direct benefit to Greater New Haven residents", key="org_q3", disabled=disabled)

    st.markdown("**Proposal Guidelines:**")
    project_title = st.text_input("Project Title*", key="org_project_title", disabled=disabled)
    desc = st.text_area("4) Detailed project description (objectives, methodology, expected outcomes)", key="org_desc", disabled=disabled)
    budget = st.text_area("5) Itemized budget (include any outside funding)", key="org_budget_text", disabled=disabled)
    budget_total = st.text_input("Budget total (USD)", key="org_budget_total", disabled=disabled)
    timeline = st.text_area("6) Project timeline (goal within 1 year of disbursement)", key="org_timeline", disabled=disabled)
    evaluation = st.text_area("7) Evaluation plan (impact/outcomes in Greater New Haven)", key="org_evaluation", disabled=disabled)

    st.markdown("**Attachments (PDF preferred):**")
    proposal_pdf = st.file_uploader("Upload Proposal / Narrative", type=["pdf","doc","docx"], key="org_proposal", disabled=disabled)
    budget_file  = st.file_uploader("Upload Budget", type=["pdf","xls","xlsx","csv"], key="org_budget_file", disabled=disabled)
    other_file   = st.file_uploader("Optional: Additional Materials (letter(s) of support, etc.)", type=["pdf","doc","docx","zip"], key="org_other_file", disabled=disabled)

    submitted = st.button("Submit Organization Application", type="primary", key="org_submit", disabled=disabled)

    payload = {
        "org_name": org_name,
        "applicant_name": applicant_name,
        "email": email,
        "phone": phone,
        "exec_dir": exec_dir,
        "exec_email": exec_email,
        "exec_phone": exec_phone,
        "mission": mission,
        "eligibility": {
            "nonprofit": eligible_nonprofit,
            "report_at_winter_meeting_2026": eligible_report,
            "benefit_gnh": eligible_benefit,
        },
        "project_title": project_title,
        "q1_issue": q1,
        "q2_align": q2,
        "q3_benefit": q3,
        "description": desc,
        "budget_text": budget,
        "budget_total": budget_total,
        "timeline": timeline,
        "evaluation": evaluation,
    }

    uploads = {}
    if submitted:
        missing = _missing_org_fields(org_name, applicant_name, email, project_title)
        if missing:
            st.warning("Please complete all required fields marked with * before submitting. Missing: " + ", ".join(missing), icon="⚠️")
            submitted = False
        elif not all([eligible_nonprofit, eligible_report, eligible_benefit]):
            st.warning("Please confirm all eligibility checkboxes.", icon="⚠️")
            submitted = False
        else:
            uploads["proposal"] = save_upload_to_storage(proposal_pdf, "org_proposal")
            uploads["budget"] = save_upload_to_storage(budget_file, "org_budget")
            uploads["other"] = save_upload_to_storage(other_file, "org_other")

    return submitted, payload, uploads, applicant_name, email, (phone or "")

def student_form() -> Tuple[bool, Dict[str, Any], Dict[str, str], str, str, str]:
    st.subheader("Medical Student Application (2025)", anchor="stu")
    submission_notice()
    st.caption(
    f"Submission deadline: **{STU_DEADLINE.strftime('%B %d, %Y at %I:%M %p %Z')}**\n\n_Required fields are marked with *_."
)

    disabled = too_late(STU_DEADLINE)
    if disabled:
        st.error("The student submission deadline has passed.")

    applicant_name = st.text_input("Applicant Name (First/Last)*", key="stu_applicant_name", disabled=disabled)
    school = st.selectbox(
        "Medical School*",
        ["— Select your school —", "Frank H. Netter MD School of Medicine at Quinnipiac University", "Yale School of Medicine"],
        index=0,
        key="stu_school",
        disabled=disabled
    )
    grad_date = st.text_input("Projected Graduation Date (MM/YYYY)", key="stu_grad_date", disabled=disabled)
    email = st.text_input("School Email*", key="stu_email", disabled=disabled)
    phone = st.text_input("Phone*", key="stu_phone", disabled=disabled)

    advisor_name = st.text_input("Advisor Name", key="stu_advisor_name", disabled=disabled)
    advisor_title = st.text_input("Advisor Title/Role", key="stu_advisor_title", disabled=disabled)
    advisor_email = st.text_input("Advisor Email", key="stu_advisor_email", disabled=disabled)

    st.markdown("**Eligibility (must confirm all):**")
    elig_enrolled = st.checkbox("I am currently enrolled at Quinnipiac (Netter) or Yale SOM.", key="stu_elig_enrolled", disabled=disabled)
    elig_report = st.checkbox("If awarded, I will present results at the NHCMA winter meeting in 2026 (date TBA).", key="stu_elig_report", disabled=disabled)

    st.markdown("**Introduction & Purpose (≈250 words each):**")
    q1 = st.text_area("1) Public health issue addressed in Greater New Haven", key="stu_q1", disabled=disabled)
    q2 = st.text_area("2) Alignment with NHCMA Foundation mission", key="stu_q2", disabled=disabled)
    q3 = st.text_area("3) Direct benefit to Greater New Haven residents", key="stu_q3", disabled=disabled)

    st.markdown("**Proposal Guidelines:**")
    project_title = st.text_input("Project Title*", key="stu_project_title", disabled=disabled)
    desc = st.text_area("4) Detailed project/research description (objectives, methodology, expected outcomes)", key="stu_desc", disabled=disabled)
    budget = st.text_area("5) Itemized budget (include any outside funding)", key="stu_budget_text", disabled=disabled)
    budget_total = st.text_input("Budget total (USD)", key="stu_budget_total", disabled=disabled)
    timeline = st.text_area("6) Timeline (goal within 1 year of disbursement)", key="stu_timeline", disabled=disabled)
    evaluation = st.text_area("7) Evaluation plan (impact on public health in Greater New Haven)", key="stu_evaluation", disabled=disabled)

    st.markdown("**Attachments (PDF preferred):**")
    proposal_pdf = st.file_uploader("Upload Proposal / Narrative", type=["pdf","doc","docx"], key="stu_proposal", disabled=disabled)
    budget_file  = st.file_uploader("Upload Budget", type=["pdf","xls","xlsx","csv"], key="stu_budget_file", disabled=disabled)
    cv_file      = st.file_uploader("Curriculum Vitae (PDF preferred)", type=["pdf","doc","docx"], key="stu_cv_file", disabled=disabled)
    support_let  = st.file_uploader("Letter of Support (optional)", type=["pdf","doc","docx"], key="stu_support_letter", disabled=disabled)

    submitted = st.button("Submit Student Application", type="primary", key="stu_submit", disabled=disabled)

    payload = {
        "applicant_name": applicant_name,
        "school": school,
        "grad_date": grad_date,
        "email": email,
        "phone": phone,
        "advisor_name": advisor_name,
        "advisor_title": advisor_title,
        "advisor_email": advisor_email,
        "eligibility": {
            "enrolled_qu_yale": elig_enrolled,
            "report_at_winter_meeting_2026": elig_report,
        },
        "project_title": project_title,
        "q1_issue": q1,
        "q2_align": q2,
        "q3_benefit": q3,
        "description": desc,
        "budget_text": budget,
        "budget_total": budget_total,
        "timeline": timeline,
        "evaluation": evaluation,
    }

    uploads = {}
    if submitted:
        # Normalize school: empty string means not selected
        school_norm = (school or "").strip()
        if school_norm == "— Select your school —":
            school_norm = ""
        missing = _missing_student_fields(applicant_name, school_norm, email, phone, project_title)
        if missing:
            st.warning("Please complete all required fields marked with * before submitting. Missing: " + ", ".join(missing), icon="⚠️")
            submitted = False
        elif not all([elig_enrolled, elig_report]):
            st.warning("Please confirm all eligibility checkboxes.", icon="⚠️")
            submitted = False
        else:
            uploads["proposal"] = save_upload_to_storage(proposal_pdf, "stu_proposal")
            uploads["budget"] = save_upload_to_storage(budget_file, "stu_budget")
            uploads["cv"] = save_upload_to_storage(cv_file, "stu_cv")
            uploads["support_letter"] = save_upload_to_storage(support_let, "stu_support")

    return submitted, payload, uploads, applicant_name, email, phone


# ----------------------------
# Admin access control
# ----------------------------
def _admin_allowed() -> bool:
    # Use a shared admin password from secrets (recommended) or env var
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or st.secrets.get("ADMIN_PASSWORD")
    if not ADMIN_PASSWORD:
        st.warning("Admin password is not configured. Set ADMIN_PASSWORD in Streamlit secrets.", icon="🔐")
        return False

    if "admin_ok" in st.session_state and st.session_state.get("admin_ok") is True:
        return True

    with st.form("admin_login_form", clear_on_submit=False):
        st.subheader("Admin Sign‑in", anchor="admin-login")
        pwd = st.text_input("Enter admin password", type="password", key="admin_pwd")
        ok = st.form_submit_button("Unlock Admin", width='content')
    if ok:
        if pwd == ADMIN_PASSWORD:
            st.session_state["admin_ok"] = True
            st.success("Admin unlocked.", icon="✅")
            return True
        else:
            st.error("Incorrect password.", icon="❌")
            return False
    return False

# ----------------------------
# Admin
# ----------------------------
def admin_panel():
    st.subheader("Admin — Submissions & Export")
    df = load_submissions_df()
    if df.empty:
        st.info("No submissions yet.")
        return

    # ===== Anchor #DF_ID_RESTORE =====
    # Ensure the 'id' column always exists — flattening can drop it
    if "id" not in df.columns:
        # Attempt recovery from index if flattening reset it
        if df.index.name == "id":
            df = df.reset_index()
        else:
            st.error("Internal error: Missing 'id' column in submissions dataset.")
            st.stop()

    # Guarantee that id is always int for consistent mapping
    df["id"] = df["id"].astype(int)
    # ===== /Anchor #DF_ID_RESTORE =====

    # Convert to Edge links before display
    for col in ["Proposal URL", "Budget URL", "Other URL"]:
        if col in df.columns:
            df[col] = df[col].apply(to_edge)

    # Single render (no duplicate below)
    st.dataframe(
        df,
        use_container_width='stretch',
        column_config={
            "Proposal URL": st.column_config.LinkColumn("Proposal URL"),
            "Budget URL":   st.column_config.LinkColumn("Budget URL"),
            "Other URL":    st.column_config.LinkColumn("Other URL"),
        },
    )

    # --- Full export (CSV + XLSX) ---
    st.markdown("**Full Export**")
    full_csv = df.to_csv(index=False).encode("utf-8")
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "Download CSV (All Submissions)",
            data=full_csv,
            file_name="nhcma_grants_submissions.csv",
            mime="text/csv",
            key="admin_dl_all_csv",
            use_container_width='stretch',
        )
    with col2:
        full_xlsx = make_excel(df)
        st.download_button(
            "Download Excel (All Submissions)",
            data=full_xlsx,
            file_name="nhcma_grants_submissions.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="admin_dl_all_xlsx",
            use_container_width='stretch',
        )

    # --- Scoring export (add URL columns) ---
    st.divider()
    st.caption("Scoring Export — key columns only")
    scoring_cols = [
        "id","track","ts_utc","applicant_name","email","phone",
        "Org Name","School","Project Title","Budget Total",
        "Proposal URL","Budget URL","Other URL",
    ]
    
    export_df = df[[c for c in scoring_cols if c in df.columns]].copy()

    # just before st.dataframe(export_df, ...)
    for col in ["Proposal URL", "Budget URL", "Other URL"]:
        if col in export_df.columns:
            export_df[col] = export_df[col].apply(to_edge)

    st.dataframe(
        export_df,
        use_container_width='stretch',
        column_config={
            "Proposal URL": st.column_config.LinkColumn("Proposal URL"),
            "Budget URL":   st.column_config.LinkColumn("Budget URL"),
            "Other URL":    st.column_config.LinkColumn("Other URL"),
        },
    )

    col3, col4 = st.columns(2)
    with col3:
        scoring_csv = export_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download CSV (Scoring Export)",
            data=scoring_csv,
            file_name="nhcma_grants_scoring_export.csv",
            mime="text/csv",
            key="admin_dl_scoring_csv",
            use_container_width='stretch',
        )
    with col4:
        scoring_xlsx = make_excel(export_df)
        st.download_button(
            "Download Excel (Scoring Export)",
            data=scoring_xlsx,
            file_name="nhcma_grants_scoring_export.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="admin_dl_scoring_xlsx",
            use_container_width='stretch',
        )

    # ----------------------------
    # Grant Decisions Section
    # ----------------------------
    st.divider()
    st.subheader("Grant Funding Decisions")

    decisions = get_all_decisions(sb_admin)

    # Merge decisions into df for convenience
    df["decision"] = df["id"].astype(str).map(
        lambda sid: decisions.get(sid, {}).get("decision")
    )
    df["amount_awarded"] = df["id"].astype(str).map(
        lambda sid: decisions.get(sid, {}).get("amount_awarded")
    )

    st.dataframe(
        df,
        use_container_width=True,
        column_config={
            "Proposal URL": st.column_config.LinkColumn("Proposal URL"),
            "Budget URL":   st.column_config.LinkColumn("Budget URL"),
            "Other URL":    st.column_config.LinkColumn("Other URL"),
        },
    )

    st.markdown("#### Select a submission to update decision")

    submission_ids = df["id"].astype(str).tolist()
    selected_id = st.selectbox("Submission ID", submission_ids)

    if selected_id:
        # -----------------------------
        # Fetch + Flatten Submission Row
        # -----------------------------
        raw_row = (
            df[df["id"].astype(str) == selected_id]
            .iloc[0]
            .to_dict()
        )

        payload = raw_row.get("payload_json", {}) or {}
        
            # DEBUG - SHOW EXACT FIELDS
        st.write("### DEBUG: raw_row keys")
        st.json(list(raw_row.keys()))

        st.write("### DEBUG: payload_json keys")
        st.json(list(payload.keys()))

        st.write("### DEBUG: payload_json contents")
        st.json(payload)


        # Start with full lowercase normalized dict
        sub_row_flat = {k.lower(): v for k, v in raw_row.items()}
        sub_row_flat.update({k.lower(): v for k, v in payload.items()})

        # ---- Canonical field wiring using real schema ----

        # Applicant name (always present)
        sub_row_flat["applicant_name"] = (
            raw_row.get("applicant_name")
            or payload.get("applicant_name")
            or ""
        )

        # Email
        sub_row_flat["email"] = (
            raw_row.get("email")
            or payload.get("email")
            or ""
        )

        # Category / Track (organization or student)
        sub_row_flat["applicant_category"] = (
            raw_row.get("track")
            or payload.get("track")
            or ""
        )

        # Project Title (stored only in payload_json)
        sub_row_flat["project_title"] = (
            payload.get("project_title")
            or ""
        )

        # Optional phone
        sub_row_flat["phone"] = (
            raw_row.get("phone")
            or payload.get("phone")
            or ""
        )

        # No program/program_name field in schema — remove that mapping entirely
        # ---- End Canonical Wiring ----

        pres = get_president_settings(sb_admin)
        current = decisions.get(selected_id)

        # -----------------------------
        # Decision controls
        # -----------------------------
        decision_choice = st.radio(
            "Decision",
            ["funded", "declined"],
            index=0 if current and current["decision"] == "funded" else 1,
        )

        amount_val = None
        if decision_choice == "funded":
            amount_val = st.number_input(
                "Award Amount",
                min_value=0.0,
                value=float(current["amount_awarded"])
                    if current and current["amount_awarded"] else 2500.0,
                step=100.0,
            )

        if st.button("Save Decision"):
            set_decision(
                sb_write,
                selected_id,
                decision_choice,
                amount_val if decision_choice == "funded" else None,
            )
            st.success("Decision saved.")
            st.experimental_rerun()

        st.markdown("---")
        st.markdown("#### Generate & Send Notification")

        # -----------------------------
        # Build letter using flattened row
        # -----------------------------
        if decision_choice == "funded":
            html = build_award_letter_html(sub_row_flat, pres, amount_val)
        else:
            html = build_decline_letter_html(sub_row_flat, pres)

        st.markdown("Preview below:")
        st.markdown(html, unsafe_allow_html=True)

        # -----------------------------
        # Send Email
        # -----------------------------
        if st.button("Send Email Notification"):
            try:
                msg = EmailMessage()
                msg["Subject"] = "NHCMA Foundation — Grant Decision"
                msg["From"] = pres["email"]
                msg["To"] = sub_row_flat.get("email")
                msg.set_content("Your email client does not support HTML.")
                msg.add_alternative(html, subtype="html")

                smtp = smtplib.SMTP(
                    os.environ.get("SMTP_HOST"),
                    int(os.environ.get("SMTP_PORT")),
                )
                smtp.starttls()
                smtp.login(os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"))
                smtp.send_message(msg)
                smtp.quit()

                st.success("Email sent successfully!")
            except Exception as e:
                st.error(f"Error sending email: {e}")

        # -----------------------------
        # Download PDF
        # -----------------------------
        if st.button("Download PDF Letter"):
            pdf_bytes = html_to_pdf_bytes(html)
            st.download_button(
                label="Download PDF",
                data=pdf_bytes,
                file_name=f"Grant_Decision_{selected_id}.pdf",
                mime="application/pdf",
            )

    # --- Booklet Builder (Admin) ---
    st.divider()
    st.subheader("📕 Build Booklets for Judging")

    from nhcma_booklet_builder import (
        get_supabase,
        build_all_booklets,
        make_signed_url,
        BUCKET_NAME,
    )

    sb = get_supabase(
        st.secrets["SUPABASE_URL"],
        st.secrets["SUPABASE_SERVICE_ROLE_KEY"]
    )

    col_a, col_b = st.columns([1, 1])
    with col_a:
        run_all = st.button("Build ALL Booklets Now", type="primary")

    # Optional future filter if 'status' column is implemented
    # with col_b:
    #     only_submitted = st.checkbox(
    #         "Only rows with status = 'submitted'",
    #         value=True
    #     )
    # where = {"status": "submitted"} if only_submitted else None

    where = None  # currently build all rows

    if run_all:

        with st.spinner("Building DOCX booklets and uploading to Storage..."):
            report = build_all_booklets(sb, where=where, start_version=1)

        st.success("Done.")
        st.dataframe(report)

        errors = [r for r in report if r.get("status") == "error"]
        if errors:
            st.warning(f"{len(errors)} booklet(s) failed to build. See logs for details.")

        ok = sum(1 for r in report if r.get("status") == "ok")
        err = sum(1 for r in report if r.get("status") == "error")
        st.write(f"Built {ok} OK / {err} errors")
        st.write("DEBUG – report count:", len(report))

    st.caption("Tip: you can re-run the build any time after submissions are frozen.")

    st.divider()
    with st.expander("🖋️ President Contact for Letters", expanded=False):

        pres = get_president_settings(sb_admin)

        colA, colB = st.columns(2)
        with colA:
            name_in = st.text_input("President Name", value=pres.get("name", ""))
            title_in = st.text_input("President Title", value=pres.get("title", ""))
        with colB:
            email_in = st.text_input("President Email (From address)", value=pres.get("email", ""))

        save_pres = st.button("Save President Info", key="save_pres_info")

        if save_pres:
            try:
                set_president_settings(sb_admin, name_in.strip(), title_in.strip(), email_in.strip())
                st.success("President information saved.")
                st.toast("Updated president contact for letters.", icon="📨")
            except Exception as e:
                st.error(f"Failed to save: {e}")

    st.divider()
    with st.expander("⚙️ Submission Deadlines", expanded=False):
        # Load current values
        _org_deadline, _stu_deadline = get_deadlines(sb_admin or sb)

        colA, colB = st.columns(2)
        with colA:
            st.caption("Organization Deadline (ET)")
            org_date = st.date_input("Date", value=_org_deadline.date(), key="org_dl_date")
            org_time = st.time_input("Time", value=_org_deadline.timetz(), key="org_dl_time")
        with colB:
            st.caption("Student Deadline (ET)")
            stu_date = st.date_input("Date ", value=_stu_deadline.date(), key="stu_dl_date")
            stu_time = st.time_input("Time ", value=_stu_deadline.timetz(), key="stu_dl_time")

        save = st.button("Save Deadlines", type="primary", key="save_deadlines_btn")
        if save:
            if not sb_admin:
                st.error("Service-role key is not configured; cannot save settings.")
            else:
                org_new = datetime.combine(org_date, org_time, tzinfo=ZoneInfo(TIMEZONE))
                stu_new = datetime.combine(stu_date, stu_time, tzinfo=ZoneInfo(TIMEZONE))
                try:
                    set_deadline(sb_admin, "organization", org_new)
                    set_deadline(sb_admin, "student", stu_new)
                    st.success("Deadlines saved.")
                    st.toast("Deadlines updated", icon="✅")
                except Exception as e:
                    st.error(f"Failed to save deadlines: {e}")


def _judging_enabled() -> bool:
    """Feature flag for the Judging module. Defaults to True so you can test immediately."""
    try:
        return bool(st.secrets.get("FEATURE_JUDGING", True))
    except Exception:
        return True


# ======== Judging Add-on (self-contained) ========
def _create_invite(judge_email: str, full_name: str, days_valid: int = 30) -> str:
    email = str(judge_email).strip().lower()
    token = str(secrets.token_urlsafe(32))
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days_valid)).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = {"email": email, "token": token, "expires_at": expires_at}
    json.dumps(payload)  # preflight

    # 🔧 ensure the judge is active
    sb_admin.table("judges").upsert(
        {
            "email": email,
            "full_name": str(full_name or "").strip(),
            "is_active": True,   # <-- REQUIRED so _resolve_token() will allow them in
        },
        on_conflict="email"
    ).execute()

    sb_admin.table("judge_invites").upsert(
        payload,
        on_conflict="email"
    ).execute()

    return token
    
def _send_invite(judge_email: str, full_name: str, app_base_url: str | None = None) -> None:
    token = _create_invite(judge_email, full_name)

    # Hard-force correct host (hotfix)
    invite_url = f"https://nhcmafoundationgrants.streamlit.app/?invite_token={token}"

    subject = "Your NHCMA Foundation Judging Link"

    # 👇 THIS MUST NOT BE COMMENTED OUT
    body_html = f"""
        <p>You're invited to judge NHCMA grants.</p>
        <p><strong>Direct link:</strong> <a href="{invite_url}">{invite_url}</a></p>
        <p>If you didn't expect this, you can ignore this message.</p>
    """

    # Send using your existing SMTP helper (to, cc, subject, html)
    send_email(judge_email, CC_EMAIL, subject, body_html)

def admin_judging_tools(app_base_url: str | None = None):
    st.subheader("Judges & Invites")

    # --- Invite form ---
    with st.form("invite_form", clear_on_submit=False):
        j_name  = st.text_input("Judge Name", key="judge_name")
        j_email = st.text_input("Judge Email", key="judge_email", placeholder="name@example.com")
        colA, colB = st.columns(2)
        with colA:
            send = st.form_submit_button("Send Invite")
        with colB:
            gen_only = st.form_submit_button("Generate Link (no email)")  # test without SMTP

    # --- Validation + actions ---
    if send or gen_only:
        name  = (j_name or "").strip()
        email = (j_email or "").strip().lower()

        # minimal email validation
        if send and (not email or "@" not in email):
            st.error("Please enter a valid judge email before sending.", icon="❌")
            return
        if send and not name:
            st.warning("No judge name provided — continuing with email only.", icon="⚠️")

        # Create/refresh token row
        token = _create_invite(email if email else "test@example.com", name or "Judge")

        # Always show the URL so you can copy/paste to test
        invite_url = f"https://nhcmafoundationgrants.streamlit.app/?invite_token={token}"
        # st.code(invite_url, language="text")
        # st.toast("Invite link generated.", icon="🔗") ----Return these if we want to display token when judge is added

        if send:
            subject = "NHCMA Foundation Grants — Your Judge Invite"
            body_html = f"""
                <p>You're invited to judge NHCMA grants.</p>
                <p><strong>Direct link:</strong> <a href="{invite_url}">{invite_url}</a></p>
                <p>If you didn't expect this, you can ignore this message.</p>
            """
            ok = send_email(email, CC_EMAIL, subject, body_html)
            if ok:
                st.success(f"Invite sent to {name or email} ({email}).", icon="✅")
            else:
                st.error("Email failed to send. You can copy the link above and send manually.", icon="✉️")

    # ----------------------------
    # Bulk Invite via CSV
    # ----------------------------
    st.divider()
    with st.expander("📥 Bulk Invite Judges (CSV)", expanded=False):
        st.markdown(
            "Upload a CSV with columns: **full_name,email[,days_valid]**. "
            "We’ll upsert judges and create fresh invite tokens."
        )
        csv_file = st.file_uploader("Upload CSV", type=["csv"], key="bulk_invite_csv")
        colX, colY = st.columns(2)
        with colX:
            do_send = st.checkbox("Send emails now", value=True, help="If unchecked, links are generated but not emailed.")
        with colY:
            default_days = st.number_input("Default link validity (days)", min_value=1, max_value=180, value=30, step=1)

        if csv_file is not None:
            try:
                _df = pd.read_csv(csv_file).fillna("")
            except Exception as e:
                st.error(f"Could not read CSV: {e}")
                _df = pd.DataFrame()

            if not _df.empty:
                # Normalize & validate
                cols = {c.strip().lower(): c for c in _df.columns}
                if "email" not in cols or "full_name" not in cols:
                    st.error("CSV must include columns: full_name, email (days_valid optional).")
                else:
                    work = _df.rename(columns={
                        cols["full_name"]: "full_name",
                        cols["email"]: "email",
                        **({"days_valid": cols.get("days_valid")} if cols.get("days_valid") else {})
                    })[["full_name", "email"] + (["days_valid"] if "days_valid" in _df.columns else [])].copy()

                    # Clean strings
                    work["full_name"] = work["full_name"].astype(str).str.strip()
                    work["email"] = work["email"].astype(str).str.strip().str.lower()
                    if "days_valid" in work.columns:
                        # Coerce invalid to NaN then fill with default
                        work["days_valid"] = pd.to_numeric(work["days_valid"], errors="coerce").fillna(default_days).astype(int)
                    else:
                        work["days_valid"] = int(default_days)

                    # Drop rows with missing essentials and dedupe by email
                    work = work[work["email"].str.contains("@", na=False)]
                    work = work.drop_duplicates(subset=["email"])

                    # Process
                    results = []
                    progress = st.progress(0.0, text="Inviting judges...")
                    total = len(work)
                    for i, row in enumerate(work.itertuples(index=False), start=1):
                        email = row.email
                        fname = row.full_name
                        dvalid = int(row.days_valid) if hasattr(row, "days_valid") and row.days_valid is not None else int(default_days)
                        try:
                            # create/refresh token and ensure judge is active
                            token = _create_invite(email, fname, days_valid=dvalid)
                            invite_url = f"https://nhcmafoundationgrants.streamlit.app/?invite_token={token}"

                            sent = False
                            if do_send:
                                subject = "NHCMA Foundation Grants — Your Judge Invite"
                                body_html = f"""
                                    <p>You're invited to judge NHCMA grants.</p>
                                    <p><strong>Direct link:</strong> <a href="{invite_url}">{invite_url}</a></p>
                                    <p>If you didn't expect this, you can ignore this message.</p>
                                """
                                sent = send_email(email, CC_EMAIL, subject, body_html)

                            results.append({
                                "Full Name": fname,
                                "Email": email,
                                "Days Valid": dvalid,
                                "Invite URL": invite_url,
                                "Email Sent": "Yes" if (do_send and sent) else ("No (link only)" if not do_send else "Failed"),
                            })
                        except Exception as e:
                            results.append({
                                "Full Name": fname,
                                "Email": email,
                                "Days Valid": dvalid,
                                "Invite URL": "",
                                "Email Sent": f"Error: {e}",
                            })
                        progress.progress(i/total, text=f"Processed {i}/{total}")

                    st.success(f"Processed {total} judge(s).")
                    res_df = pd.DataFrame(results)
                    st.dataframe(res_df, use_container_width='stretch')
                    st.download_button(
                        "Download Results (CSV)",
                        res_df.to_csv(index=False).encode("utf-8"),
                        "judge_bulk_invite_results.csv",
                        "text/csv",
                        key="bulk_invite_results_dl",
                        use_container_width='stretch',
                    )
            else:
                st.info("CSV appears empty.")



    # ----------------------------
    # Scoring Tally (Ranked by Category)
    # ----------------------------
    st.divider()
    st.subheader("Scoring Tally — Ranked by Category")

    try:
        s = sb_admin.table("scores").select("*").execute().data or []
        sc = pd.DataFrame(s)
    except Exception:
        sc = pd.DataFrame()

    subs = load_submissions_df()
    if sc.empty or subs is None or subs.empty:
        st.info("No scores yet.")
    else:
        # Merge scores with submission info (title, org/school, contact)
        merged = sc.merge(
            subs[
                ["id","track","Q: project_title","Q: org_name","Q: school",
                 "applicant_name","email","phone"]
            ].rename(columns={
                "id": "submission_id",
                "Q: project_title": "Project Title",
                "Q: org_name": "Org Name",
                "Q: school": "School",
            }),
            on=["submission_id","track"], how="left"
        )

        # Aggregate averages
        agg = (merged.groupby(
                    ["track","submission_id","Project Title","Org Name","School",
                     "applicant_name","email","phone"], dropna=False)
                      .agg(avg_total=("total_points","mean"),
                           n_scores=("total_points","count"))
                      .reset_index())

        # Add rank per track
        agg["Rank"] = agg.groupby("track")["avg_total"] \
                         .rank(method="dense", ascending=False).astype(int)

        agg = agg.sort_values(["track","Rank"], ascending=[True, True])

        # Round for display
        agg["avg_total"] = agg["avg_total"].round(2)

        st.dataframe(
            agg[["track","Rank","Project Title","Org Name","School",
                 "applicant_name","email","phone","avg_total","n_scores"]],
            use_container_width='stretch'
        )

        st.download_button(
            "Download Ranked Tally (CSV)",
            agg.to_csv(index=False).encode("utf-8"),
            "nhcma_scoring_tally_ranked.csv",
            "text/csv",
            use_container_width='stretch',
        )


    # ----------------------------
    # Detailed Scores by Judge
    # ----------------------------
    st.divider()
    st.subheader("Detailed Scores by Judge")

    try:
        raw_scores = sb_admin.table("scores").select("*").execute().data or []
        sc = pd.DataFrame(raw_scores)
    except Exception:
        sc = pd.DataFrame()

    subs = load_submissions_df()

    if sc.empty or subs is None or subs.empty:
        st.info("No detailed scores available yet.")
    else:
        sub_cols = ["id","track","Q: project_title","Q: org_name","Q: school"]
        sub_map = subs[sub_cols].rename(columns={
            "id": "submission_id",
            "Q: project_title": "Project Title",
            "Q: org_name": "Org Name",
            "Q: school": "School",
        })
        sc = sc.merge(sub_map, on=["submission_id","track"], how="left")

        try:
            jrows = sb_admin.table("judges").select("id,full_name,email").execute().data or []
            jd = pd.DataFrame(jrows).rename(columns={
                "id": "judge_id",
                "full_name": "Judge",
                "email": "Judge Email"
            })
            sc = sc.merge(jd, on="judge_id", how="left")
        except Exception:
            pass

        preferred = [
            "Judge","Judge Email",
            "submission_id","track","Project Title","Org Name","School",
            "total_points","innovativeness","feasibility","alignment","community_eval","clarity","budget",
            "comments","submitted_at"
        ]
        cols = [c for c in preferred if c in sc.columns]

        # Optional tidy sort
        if "Judge" in sc.columns and "submission_id" in sc.columns:
            sc = sc.sort_values(["Judge","track","submission_id","submitted_at"], ascending=[True, True, True, True])

        st.dataframe(sc[cols], use_container_width='stretch')
        st.download_button(
            "Download Detailed Scores (CSV)",
            sc[cols].to_csv(index=False).encode("utf-8"),
            "nhcma_detailed_scores.csv",
            "text/csv",
            use_container_width='stretch',
        )


def _resolve_token(judge_token: str):
    try:
        inv = sb_admin.table("judge_invites").select("*").eq("token", judge_token).execute().data
    except Exception:
        return None
    if not inv:
        return None
    inv = inv[0]
    try:
        judge = sb_admin.table("judges").select("*").eq("email", inv["email"]).single().execute().data
    except Exception:
        return None
    if not judge or not judge.get("is_active"):
        return None
    return {"judge_id": judge["id"], "email": judge["email"], "name": judge["full_name"]}

def _scoring_criteria_for_track(track: str):
    if track == "student":
        return [
            ("feasibility",    "Feasibility of Project Completion Within 1 Year (1–5)"),
            ("alignment",      "Alignment with NHCMA Foundation Mission/Goals (1–5)"),
            ("community_eval", "Addresses Specific Community Need & Evaluation (1–5)"),
            ("clarity",        "Clarity & Comprehensiveness (1–5)"),
            ("budget",         "Budget Appropriateness (1–5)"),
        ]
    else:
        return [
            ("innovativeness", "Innovativeness (1–5)"),
            ("feasibility",    "Feasibility of Project Completion Within 1 Year (1–5)"),
            ("alignment",      "Alignment with NHCMA Foundation Mission/Goals (1–5)"),
            ("community_eval", "Addresses Specific Community Need & Evaluation (1–5)"),
            ("clarity",        "Clarity & Comprehensiveness (1–5)"),
            ("budget",         "Budget Appropriateness (1–5)"),
        ]

def _score_total(track: str, vals: dict) -> int:
    keys = [k for k, _ in _scoring_criteria_for_track(track)]
    return int(sum(int(vals.get(k, 0) or 0) for k in keys))

def judging_portal():
    # Require a judge session established via invite link
    who = st.session_state.get("judge_session")
    if not who:
        st.info("To access judging, please use your personal invite link.")
        return

    # Load submissions
    df = load_submissions_df()
    if df is None or df.empty:
        st.info("No submissions available yet.")
        return

    # Normalize display columns
    df["Project Title"] = df.get("Q: project_title", df.get("project_title", ""))
    df["Org Name"]      = df.get("Q: org_name", df.get("org_name", ""))
    df["School"]        = df.get("Q: school", df.get("school", ""))

    # Pick a default track that actually has rows
    tracks_present = [t for t in ["student", "organization"] if t in set(df["track"].dropna().tolist())]
    if not tracks_present:
        st.info("Submissions table is present, but no recognized tracks ('student'/'organization') were found.")
        st.dataframe(df, use_container_width='stretch')
        return

    default_track = tracks_present[0]
    track = st.radio("Select track", ["student", "organization"], horizontal=True,
                     index=["student", "organization"].index(default_track))

    # Filter to chosen track
    sdf = df[df["track"] == track].copy()
    if sdf.empty:
        st.info(f"No submissions for the **{track}** track yet.")
        return

    # Convert columns to Edge links before displaying
    for col in ["Proposal URL", "Budget URL", "Other URL"]:
        if col in sdf.columns:
            sdf[col] = sdf[col].apply(to_edge)

    st.dataframe(
        sdf[["id","Project Title","Org Name","School","Proposal URL","Budget URL"]].fillna(""),
        use_container_width='stretch',
        column_config={
            "Proposal URL": st.column_config.LinkColumn("Proposal URL"),
            "Budget URL":   st.column_config.LinkColumn("Budget URL"),
        },
        hide_index=True
    )

    # Submission chooser
    options = [(int(r["id"]), f"#{int(r['id'])}: {r['Project Title'] or '(untitled)'}") for _, r in sdf.iterrows()]
    if not options:
        st.info("No selectable submissions found for this track.")
        return
    submission_id = st.selectbox("Choose a submission", options, format_func=lambda t: t[1], index=0)[0]
    
    # --- Booklet download (DOCX) for this submission ---
    from nhcma_booklet_builder import get_supabase, make_signed_url, BUCKET_NAME

    sb_srv = get_supabase(
        st.secrets["SUPABASE_URL"],
        st.secrets["SUPABASE_SERVICE_ROLE_KEY"],
    )

    # Locate this submission's row to get the stored booklet path
    selected_row = sdf.loc[sdf["id"] == submission_id].iloc[0]
    path = selected_row.get("booklet_docx_path")

    if path and str(path).strip().lower() not in {"", "none", "null"}:
        url = make_signed_url(sb_srv, BUCKET_NAME, path, expires_in_seconds=24*3600)
        st.link_button("⬇️ Download Booklet (DOCX)", url, use_container_width='stretch')
    else:
        st.info("No booklet for this submission yet.")


    # Load any previous score by this judge
    prev = []
    try:
        prev = sb_admin.table("scores").select("*") \
            .eq("submission_id", submission_id).eq("judge_id", who["judge_id"]).execute().data or []
    except Exception:
        prev = []
    prev = prev[0] if prev else {}

    # ---- Live-scoring block (no form) ----
    st.markdown("### Score this submission")

    # Comments persist per submission
    comments_key = f"comments_{submission_id}"
    if comments_key not in st.session_state:
        st.session_state[comments_key] = prev.get("comments") or ""
    comments = st.text_area("Comments (optional, visible to committee)", key=comments_key)

    # Scoring inputs
    for key, label in _scoring_criteria_for_track(track):
        state_key = f"{key}_{submission_id}"
        if state_key not in st.session_state:
            st.session_state[state_key] = int(prev.get(key, 3) or 3)
        st.number_input(label, min_value=1, max_value=5, step=1, key=state_key)

    # Compute total live (updates instantly on any change)
    live_vals = {k: int(st.session_state[f"{k}_{submission_id}"]) for k, _ in _scoring_criteria_for_track(track)}
    total = _score_total(track, live_vals)
    st.metric("Total Points", total)

    # Save current values
    if st.button("Save Score", type="primary", key=f"save_{submission_id}"):
        payload = {
            "submission_id": submission_id,
            "judge_id": who["judge_id"],
            "track": track,
            **live_vals,
            "total_points": total,
            "comments": st.session_state[comments_key],
            "submitted_at": datetime.utcnow().isoformat()
        }
        try:
            sb_admin.table("scores").upsert(payload, on_conflict="submission_id,judge_id").execute()
            st.success("Score saved.")
            st.toast("Score saved ✅", icon="✅")
        except Exception as e:
            st.error(f"Failed to save: {e}")
            
# --- Early invite-token resolver (runs before UI) ---

def _consume_invite_from_query():
    """If URL has ?invite_token=..., resolve it.
       If first-time: set PIN.
       Otherwise: create cookie session and sign in, then clear query + rerun."""
    try:
        params = dict(st.query_params) if hasattr(st, "query_params") else st.experimental_get_query_params()
        raw = params.get("invite_token")
        token = (raw[0] if isinstance(raw, list) else raw) if raw else None
        if not token:
            return

        who = _resolve_token(token)  # -> {judge_id, email, name} or None
        if not who:
            st.warning("Invite link invalid or expired.")
            return

        # First-time PIN setup?
        j = _db_judge_by_id(who["judge_id"]) or {}
        if not j.get("pin_hash"):
            st.info(f"Welcome, {who['name']}. Create a 4–8 digit PIN for future sign-ins (no invite link needed).")
            pin1 = st.text_input("Choose a PIN (digits only, 4–8)", type="password", key="pin_new_1")
            pin2 = st.text_input("Confirm PIN", type="password", key="pin_new_2")
            if st.button("Save PIN", type="primary", key="btn_save_pin"):
                if not pin1 or not pin1.isdigit() or not (4 <= len(pin1) <= 8) or pin1 != pin2:
                    st.error("PIN must be 4–8 digits (numbers only) and both fields must match.")
                else:
                    _db_set_pin(who["judge_id"], hash_pin(pin1))
                    # Create session + cookie
                    sess = _db_create_session(who["judge_id"])
                    set_cookie_session(sess)
                    st.session_state["judge_session"] = {"judge_id": who["judge_id"], "email": who["email"], "name": who["name"]}
                    try:
                        if hasattr(st, "query_params"):
                            st.query_params.clear()
                    except Exception:
                        pass
                    st.success("PIN set. You’re signed in.")
                    st.rerun()
            st.stop()  # Stay on PIN screen until saved

        # Existing PIN → sign in with session + cookie
        sess = _db_create_session(who["judge_id"])
        set_cookie_session(sess)
        st.session_state["judge_session"] = {"judge_id": who["judge_id"], "email": who["email"], "name": who["name"]}
        try:
            if hasattr(st, "query_params"):
                st.query_params.clear()
        except Exception:
            pass
        st.success(f"Welcome, {who['name']}! You’re signed in.")
        st.rerun()

    except Exception:
        st.error("Could not process invite. Please try again or request a new invite link.")


# Run resolver before any UI renders
_consume_invite_from_query()

# Header with logo + title
col_logo, col_title = st.columns([1, 5], vertical_alignment="center")
with col_logo:
    if os.path.exists("assets/logo.jpg"):
        st.image("assets/logo.jpg", width='stretch')
    else:
        st.write("")  # blank if logo not present
with col_title:
    st.title(APP_TITLE)
    st.write("**Grant Amount:** Up to $2,500 • **Submission Year:** 2025")

# Instructions / Notice
# st.warning(
    # "Please have all documentation ready before you begin. "
    # "You must complete and submit the application in one session; "
    # "if you leave before submitting, you will need to start over.",
    # icon="📝"
# )
st.info(
    "Questions? Email the NHCMA Foundation at **nhcma@lutinemanagement.com**.",
    icon="✉️"
)
st.divider()

if _judging_enabled():
    tab1, tab2, tab3, tab4 = st.tabs([
        "Apply — Organizations", "Apply — Medical Students", "Admin", "Judging"
    ])
else:
    tab1, tab2, tab3 = st.tabs([
        "Apply — Organizations", "Apply — Medical Students", "Admin"
    ])

# Load deadlines from DB (fallback to defaults)
ORG_DEADLINE, STU_DEADLINE = get_deadlines(sb_admin or sb)

with tab1:
    submitted, payload, uploads, name, email, phone = org_form()
    if submitted:
        rid = insert_submission("organization", name, email, phone, payload, uploads)
        if rid:
            st.success("Thank you! Your organization application has been submitted.")
            # Send confirmation email
            subject = "NHCMA Foundation — Organization Application Received (2025)"
            html = build_confirmation_email("organization", payload, rid)
            send_email(email, CC_EMAIL, subject, html)
        else:
            st.error("There was a problem saving your submission. Please try again or contact support.")

with tab2:
    submitted, payload, uploads, name, email, phone = student_form()
    if submitted:
        rid = insert_submission("student", name, email, phone, payload, uploads)
        if rid:
            st.success("Thank you! Your student application has been submitted.")
            # Send confirmation email
            subject = "NHCMA Foundation — Student Application Received (2025)"
            html = build_confirmation_email("student", payload, rid)
            send_email(email, CC_EMAIL, subject, html)
        else:
            st.error("There was a problem saving your submission. Please try again or contact support.")

with tab3:
    if _admin_allowed():
        admin_panel()
        if _judging_enabled():
            st.divider()
            st.caption("Judging — Invites & Tally")
            admin_judging_tools()
    else:
        st.info("Admin locked. Enter the admin password above to view Admin tools.")
        # Do NOT st.stop(); allow Judging tab to render

# --- Judging tab render ---
if _judging_enabled():
    with tab4:
        # 1) If we already have a session, show logout
        if "judge_session" in st.session_state:
            if st.sidebar.button("Log out"):
                clear_cookie_session()
                st.session_state.pop("judge_session", None)
                st.rerun()

        # 2) Otherwise try cookie first; then email+PIN fallback
        if "judge_session" not in st.session_state:
            tok = get_cookie_session()
            who = _db_validate_session(tok) if tok else None
            if who:
                st.session_state["judge_session"] = who
            else:
                with st.expander("Judge Sign In", expanded=True):
                    email = st.text_input("Email", key="judge_login_email")
                    pin   = st.text_input("PIN", type="password", key="judge_login_pin")
                    if st.button("Sign in", key="judge_login_btn"):
                        j = _db_judge_by_email(email or "")
                        if not j or not j.get("pin_hash") or not verify_pin(pin or "", j["pin_hash"]):
                            st.error("Invalid email or PIN.")
                        else:
                            sess = _db_create_session(j["id"])
                            set_cookie_session(sess)
                            st.session_state["judge_session"] = {"judge_id": j["id"], "email": j["email"], "name": j["full_name"]}
                            st.rerun()
                st.stop()

        # 3) Safe to render portal
        try:
            judging_portal()
        except Exception as e:
            st.error("Judging tab failed to render.")
            st.exception(e)


st.caption("© 2025 New Haven County Medical Association Foundation")
