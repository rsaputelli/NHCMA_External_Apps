
# NHCMA Grants Streamlit App — Supabase + Keys + Email + Header Notice
# - Unique keys per widget to avoid duplicate id errors
# - Header with logo (logo.jpg) and instructions/notice
# - Email confirmations via SMTP (Office365) to applicant with CC to nhcma@lutinemanagement.org
# - Supabase (DB + Storage) for submissions & uploads
#
# Required secrets (Streamlit Cloud -> Secrets):
# SUPABASE_URL = https://<ref>.supabase.co
# SUPABASE_ANON_KEY = <anon>
# SUPABASE_BUCKET = nhcma-uploads
# SMTP_HOST = smtp.office365.com
# SMTP_PORT = 587
# SMTP_USER = <your 365 user/email>
# SMTP_PASSWORD = <app password or auth token>
# SMTP_FROM_EMAIL = <from email shown to recipients>
# SMTP_FROM_NAME = NHCMA Foundation Grants
#
# Optional: if logo.jpg is in the repo root, it will be displayed.

import os, json, smtplib
from email.message import EmailMessage
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, Tuple, Optional

import streamlit as st
import pandas as pd
from supabase import create_client, Client

APP_TITLE = "NHCMA Foundation — 2025 Public Health Innovation Grants"
TIMEZONE = "America/New_York"

# Deadlines (ET)
ORG_DEADLINE = datetime(2025, 10, 17, 16, 59, tzinfo=ZoneInfo(TIMEZONE))
STU_DEADLINE = datetime(2025, 10, 19, 23, 59, tzinfo=ZoneInfo(TIMEZONE))

# Supabase config
SUPABASE_URL = os.getenv("SUPABASE_URL") or st.secrets.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or st.secrets.get("SUPABASE_ANON_KEY")
BUCKET = os.getenv("SUPABASE_BUCKET") or st.secrets.get("SUPABASE_BUCKET", "nhcma-uploads")

@st.cache_resource(show_spinner=False)
def supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        st.error("Missing SUPABASE_URL or SUPABASE_ANON_KEY in secrets.")
        st.stop()
    return create_client(str(SUPABASE_URL), str(SUPABASE_ANON_KEY))

sb = supabase_client()

# SMTP config
SMTP_HOST = os.getenv("SMTP_HOST") or st.secrets.get("SMTP_HOST", "smtp.office365.com")
SMTP_PORT = int(os.getenv("SMTP_PORT") or st.secrets.get("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER") or st.secrets.get("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD") or st.secrets.get("SMTP_PASSWORD")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL") or st.secrets.get("SMTP_FROM_EMAIL") or SMTP_USER
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME") or st.secrets.get("SMTP_FROM_NAME", "NHCMA Foundation Grants")

def too_late(deadline: datetime) -> bool:
    now = datetime.now(ZoneInfo(TIMEZONE))
    return now > deadline

def save_upload_to_storage(file, prefix: str) -> str:
    if file is None:
        return ""
    safe_name = file.name.replace("/", "_").replace("\\", "_")
    key = f"{prefix}/{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{safe_name}"
    try:
        sb.storage.from_(BUCKET).upload(key, file, file_options={"content-type": file.type})
    except Exception as e:
        st.warning(f"Upload failed for {safe_name}: {e}")
        return ""
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
        res = sb.table("submissions").insert(data).execute()
        if getattr(res, "data", None):
            return res.data[0].get("id")
    except Exception as e:
        st.error(f"Error saving submission: {e}")
    return None

def load_submissions_df() -> pd.DataFrame:
    try:
        res = sb.table("submissions").select("*").order("id", desc=True).execute()
        rows = res.data or []
    except Exception as e:
        st.error(f"Error loading submissions: {e}")
        rows = []
    df = pd.DataFrame(rows)
    if not df.empty:
        flat = []
        for p in df.get("payload_json", []):
            p = p or {}
            flat.append({
                "Org Name": p.get("org_name",""),
                "Project Title": p.get("project_title",""),
                "School": p.get("school",""),
                "Advisor Name": p.get("advisor_name",""),
                "Budget Total": p.get("budget_total",""),
            })
        df = pd.concat([df, pd.DataFrame(flat)], axis=1)
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

# ----------------------------
# Forms with unique keys
# ----------------------------
def org_form() -> Tuple[bool, Dict[str, Any], Dict[str, str], str, str, str]:
    st.subheader("Organization Application (2025)", anchor="org")
    st.caption("Submission deadline: **October 17, 2025 at 4:59 PM ET**")

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
    eligible_report = st.checkbox("Recipient will present final report at the NHCMA winter meeting in 2025 (date TBA).", key="org_elig_report", disabled=disabled)
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
            "report_at_winter_meeting_2025": eligible_report,
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
        required = [org_name, applicant_name, email, project_title]
        if not all(x and str(x).strip() for x in required):
            st.warning("Please complete all required fields marked with * before submitting.", icon="⚠️")
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
    st.caption("Submission deadline: **October 19, 2025 at 11:59 PM ET**")

    disabled = too_late(STU_DEADLINE)
    if disabled:
        st.error("The student submission deadline has passed.")

    applicant_name = st.text_input("Applicant Name (First/Last)*", key="stu_applicant_name", disabled=disabled)
    school = st.selectbox(
        "Medical School*",
        ["", "Frank H. Netter MD School of Medicine at Quinnipiac University", "Yale School of Medicine"],
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
    elig_report = st.checkbox("If awarded, I will present results at the NHCMA winter meeting in 2025 (date TBA).", key="stu_elig_report", disabled=disabled)

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
            "report_at_winter_meeting_2025": elig_report,
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
        required = [applicant_name, school, email, phone, project_title]
        if not all(x and str(x).strip() for x in required):
            st.warning("Please complete all required fields marked with * before submitting.", icon="⚠️")
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
# Admin
# ----------------------------
def admin_panel():
    st.subheader("Admin — Submissions & Export")
    df = load_submissions_df()
    if df.empty:
        st.info("No submissions yet.")
        return

    st.dataframe(df, use_container_width=True)
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV (All Submissions)", data=csv, file_name="nhcma_grants_submissions.csv", mime="text/csv", key="admin_dl_all")

    scoring_cols = ["id","track","ts_utc","applicant_name","email","phone","Org Name","School","Project Title","Budget Total"]
    export_df = df[[c for c in scoring_cols if c in df.columns]].copy()
    st.divider()
    st.caption("Scoring Export — key columns only")
    st.dataframe(export_df, use_container_width=True)
    csv2 = export_df.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV (Scoring Export)", data=csv2, file_name="nhcma_grants_scoring_export.csv", mime="text/csv", key="admin_dl_scoring")

# ----------------------------
# Header / Main
# ----------------------------
st.set_page_config(page_title="NHCMA Grants 2025", layout="wide")

# Header with logo + title
col_logo, col_title = st.columns([1, 5], vertical_alignment="center")
with col_logo:
    if os.path.exists("logo.jpg"):
        st.image("logo.jpg", use_container_width=True)
    else:
        st.write("")  # blank if logo not present
with col_title:
    st.title(APP_TITLE)
    st.write("**Grant Amount:** Up to $2,500 • **Submission Year:** 2025")

# Instructions / Notice
st.warning(
    "Please have all documentation ready before you begin. "
    "You must complete and submit the application in one session; "
    "if you leave before submitting, you will need to start over.",
    icon="📝"
)
st.info(
    "Questions? Email the NHCMA Foundation at **nhcma@lutinemanagement.com**.",
    icon="✉️"
)
st.divider()

tab1, tab2, tab3 = st.tabs(["Apply — Organizations", "Apply — Medical Students", "Admin"])

with tab1:
    submitted, payload, uploads, name, email, phone = org_form()
    if submitted:
        rid = insert_submission("organization", name, email, phone, payload, uploads)
        if rid:
            st.success("Thank you! Your organization application has been submitted.")
            # Send confirmation email
            subject = "NHCMA Foundation — Organization Application Received (2025)"
            html = build_confirmation_email("organization", payload, rid)
            send_email(email, "nhcma@lutinemanagement.org", subject, html)
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
            send_email(email, "nhcma@lutinemanagement.org", subject, html)
        else:
            st.error("There was a problem saving your submission. Please try again or contact support.")

with tab3:
    admin_panel()

st.caption("© 2025 New Haven County Medical Association Foundation")
