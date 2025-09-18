# pages/Poster_Submissions.py
# NHCMA — Research Poster Presentations (Student / Resident / Fellow)
# - Uses same Supabase project/secrets as the Grants app
# - Private bucket: nhcma-posters (signed URLs)
# - Email confirmations via Office365 SMTP to submitter + CC to nhcma@lutinemanagement.com

import os
import io
import smtplib
from email.message import EmailMessage
from datetime import datetime
from typing import Dict, Any, Optional
from zoneinfo import ZoneInfo
from pathlib import Path

import streamlit as st
import pandas as pd
from supabase import create_client

# ---------- App meta / page config ----------
APP_TITLE = "NHCMA — Research Poster Presentations"
TIMEZONE = "America/New_York"
POSTERS_BUCKET = "nhcma-posters"

st.set_page_config(page_title=APP_TITLE, page_icon="🧪", layout="wide")

# ---------- Secrets / Supabase config ----------
_sb = st.secrets.get("supabase", {})  # supports nested [supabase] too
SUPABASE_URL = (
    os.getenv("SUPABASE_URL")
    or st.secrets.get("SUPABASE_URL")
    or _sb.get("url")
)
SUPABASE_ANON_KEY = (
    os.getenv("SUPABASE_ANON_KEY")
    or st.secrets.get("SUPABASE_ANON_KEY")
    or _sb.get("anon_key")
)

# Guard: fail early with a friendly message if secrets are missing
if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    st.error(
        "Supabase configuration is missing. Please set **SUPABASE_URL** and **SUPABASE_ANON_KEY** "
        "in Streamlit Secrets (flat or under `[supabase]`)."
    )
    st.stop()

# Create clients
sb = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
SERVICE_ROLE_KEY = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")
sb_admin = create_client(SUPABASE_URL, SERVICE_ROLE_KEY) if SERVICE_ROLE_KEY else None

# ---------- SMTP config (reuse grants secrets) ----------
_smtp = st.secrets.get("smtp", {})
SMTP_HOST = os.getenv("SMTP_HOST") or st.secrets.get("SMTP_HOST") or _smtp.get("host", "smtp.office365.com")
SMTP_PORT = int(os.getenv("SMTP_PORT") or st.secrets.get("SMTP_PORT") or _smtp.get("port", 587))
SMTP_USER = os.getenv("SMTP_USER") or st.secrets.get("SMTP_USER") or _smtp.get("user")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD") or st.secrets.get("SMTP_PASSWORD") or _smtp.get("password")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL") or st.secrets.get("SMTP_FROM_EMAIL") or _smtp.get("from_addr") or SMTP_USER
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME") or st.secrets.get("SMTP_FROM_NAME") or _smtp.get("from_name", "NHCMA Foundation")
CC_EMAIL = "nhcma@lutinemanagement.com"

# ---------- Header with logo ----------
def render_header():
    logo_path = None
    for p in ("assets/logo.jpg", "logo.jpg"):
        if Path(p).exists():
            logo_path = p
            break

    left, right = st.columns([1, 3], vertical_alignment="center")
    with left:
        if logo_path:
            st.image(str(logo_path), width=200)
        else:
            st.write("")  # spacer
    with right:
        st.title(APP_TITLE)
        st.markdown(
            "Please have your information ready before beginning. "
            "Questions: **nhcma@lutinemanagement.com**."
        )

render_header()
st.caption("Collection form for Student, Resident, and Fellow research posters (no judging).")

# ---------- Helpers ----------
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

def save_upload_to_storage(file, prefix: str) -> str:
    """Upload a Streamlit UploadedFile to Supabase Storage (private) and return a signed URL."""
    if not file:
        return ""
    safe_name = file.name.replace("/", "_").replace("\\", "_")
    key = f"{prefix}/{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{safe_name}"

    # Read bytes from UploadedFile
    try:
        file_bytes = file.getvalue()
    except Exception:
        try:
            file_bytes = file.read()
        except Exception:
            file_bytes = None
    if not file_bytes:
        st.warning(f"Upload failed for {safe_name}: could not read file bytes")
        return ""

    # Upload BYTES; headers must be strings
    content_type = getattr(file, "type", None) or "application/pdf"
    file_options = {"content-type": str(content_type), "upsert": "true"}
    try:
        sb.storage.from_(POSTERS_BUCKET).upload(key, file_bytes, file_options=file_options)
    except Exception as e:
        st.warning(f"Upload failed for {safe_name}: {e}")
        return ""

    # Signed URL (private bucket)
    try:
        signed = sb.storage.from_(POSTERS_BUCKET).create_signed_url(key, expires_in=60*60*24*7)
        if isinstance(signed, dict):
            return signed.get("signedURL") or signed.get("signed_url") or ""
        return str(signed)
    except Exception:
        return ""

def insert_poster(payload: Dict[str, Any]) -> Optional[int]:
    """Insert poster row; prefer service role (bypasses RLS), fallback to anon."""
    client = sb_admin or sb
    try:
        res = client.table("posters").insert(payload).execute()
        if getattr(res, "data", None):
            return res.data[0].get("id")
    except Exception as e:
        st.error(f"Error saving poster: {e}")
    return None

# ---------- Form (grouped layout) ----------
with st.form("poster_form", clear_on_submit=True):
    st.markdown("### Submit Your Poster")

    # Row 1: Lead author + contact  |  Category
    colA, colB = st.columns([2, 1])
    with colA:
        st.markdown("**Lead Author**")
        lead_author = st.text_input("Full Name*", key="lead_name")
        contact_email = st.text_input("Contact Email*", key="lead_email")
        inst_lead = st.text_input("Institution (Lead)", key="lead_inst")
    with colB:
        st.markdown("**Category**")
        category = st.selectbox("Select*", ["Student", "Resident", "Fellow"], key="cat")

    st.divider()

    # Row 2: Co-authors (optional)
    st.markdown("**Co-Authors (optional, up to 3)**")
    c1, c2, c3 = st.columns(3)
    with c1:
        co1 = st.text_input("Co-Author 1", key="co1")
        inst_co1 = st.text_input("Institution 1", key="co1i")
    with c2:
        co2 = st.text_input("Co-Author 2", key="co2")
        inst_co2 = st.text_input("Institution 2", key="co2i")
    with c3:
        co3 = st.text_input("Co-Author 3", key="co3")
        inst_co3 = st.text_input("Institution 3", key="co3i")

    st.divider()

    # Row 3: Project details
    st.markdown("**Project**")
    title = st.text_input("Title of Project*", key="title")
    abstract = st.text_area(
        "Brief Abstract* (≤ 250 words)",
        height=180,
        help="Plain text, up to ~250 words.",
        key="abstract",
    )

    # Row 4: Poster file
    st.markdown("**Poster File (optional)**")
    poster_file = st.file_uploader("Upload PDF", type=["pdf"], key="poster_pdf")

    submit = st.form_submit_button("Submit Poster")

# Validation + submit
# Validation + submit (DE-DUPED)
if submit:
    required = [category, lead_author, title, abstract, contact_email]
    if not all((x or "").strip() for x in required):
        st.warning("Please complete all required fields marked with *.", icon="⚠️")
    elif len(abstract.split()) > 250:
        st.warning("Abstract appears to exceed 250 words. Please shorten.", icon="⚠️")
    else:
        # build a stable token for this exact submission
        token_parts = [
            category.strip(),
            (lead_author or "").strip(),
            (contact_email or "").strip(),
            (title or "").strip(),
            str(len((abstract or "").strip())),
            poster_file.name if poster_file else "",
        ]
        submission_token = "|".join(token_parts)

        # skip if we already processed this token
        if st.session_state.get("last_poster_token") == submission_token:
            st.info("This submission was already received. (Duplicate prevented)")
        else:
            poster_url = save_upload_to_storage(poster_file, prefix="posters") if poster_file else ""
            payload = {
                "category": category,
                "lead_author": lead_author.strip(),
                "coauthor1": (co1 or "").strip(),
                "coauthor2": (co2 or "").strip(),
                "coauthor3": (co3 or "").strip(),
                "institution_lead": (inst_lead or "").strip(),
                "institution_co1": (inst_co1 or "").strip(),
                "institution_co2": (inst_co2 or "").strip(),
                "institution_co3": (inst_co3 or "").strip(),
                "title": title.strip(),
                "abstract": abstract.strip(),
                "poster_url": poster_url,
                "contact_email": (contact_email or "").strip(),
            }
            rid = insert_poster(payload)
            if rid:
                st.session_state["last_poster_token"] = submission_token
                st.success("Thank you! Your poster has been submitted.")
                when = datetime.now(ZoneInfo(TIMEZONE)).strftime("%b %d, %Y %I:%M %p %Z")
                subj = "NHCMA — Poster Submission Received"
                link_html = f'<br><strong>Poster file:</strong> <a href="{poster_url}">View</a>' if poster_url else ""
                html = f"""
                    <p>Dear {lead_author},</p>
                    <p>Thank you for submitting your <strong>{category}</strong> research poster to the NHCMA Foundation.</p>
                    <p>
                        <strong>Title:</strong> {title}<br>
                        <strong>Submitted:</strong> {when}
                        {link_html}
                    </p>
                    <p>We will contact you if additional information is needed.<br>
                    Questions: <a href="mailto:nhcma@lutinemanagement.com">nhcma@lutinemanagement.com</a></p>
                    <p>— NHCMA Foundation</p>
                """
                send_email(contact_email, CC_EMAIL, subj, html)
                st.stop()  # end this run cleanly
            else:
                st.error("There was a problem saving your submission. Please try again or contact support.")


# ---------- Admin (password-gated) ----------

def admin_panel():
    client = sb_admin or sb
    try:
        res = client.table("posters").select("*").order("id", desc=True).execute()
        rows = res.data or []
    except Exception as e:
        st.error(f"Error loading posters: {e}")
        rows = []
    df = pd.DataFrame(rows)

    # Put useful columns first if present
    if not df.empty:
        first = [c for c in [
            "id", "created_at", "category", "lead_author", "contact_email",
            "title", "abstract", "poster_url"
        ] if c in df.columns]
        df = df[first + [c for c in df.columns if c not in first]]

    with st.expander("Admin — Submissions & Export", expanded=True):
        st.dataframe(
            df,
            use_container_width=True,
            column_config={"poster_url": st.column_config.LinkColumn("Poster URL")},
        )
        if not df.empty:
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download CSV (Posters)",
                data=csv,
                file_name="nhcma_posters.csv",
                mime="text/csv",
                use_container_width=True,
            )

def _admin_allowed() -> bool:
    """Password gate for Admin. Set ADMIN_PASSWORD in Streamlit secrets."""
    PW = st.secrets.get("ADMIN_PASSWORD")
    if not PW:
        st.error("ADMIN_PASSWORD is not set in secrets.")
        return False

    # already logged in?
    if st.session_state.get("admin_ok"):
        c1, c2 = st.columns([1, 5])
        with c1:
            if st.button("Logout", key="admin_logout"):
                st.session_state.pop("admin_ok", None)
                st.rerun()

        return True

    with st.form("admin_login", clear_on_submit=False):
        pw = st.text_input("Enter admin password", type="password", key="admin_pw")
        ok = st.form_submit_button("Login")
    if ok:
        if (pw or "").strip() == str(PW):
            st.session_state["admin_ok"] = True
            st.success("Welcome, admin.")
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()

# Gate + render
st.divider()
st.subheader("Admin")
if _admin_allowed():
    admin_panel()
