
# NHCMA Grants Streamlit App — Supabase Edition
# - Two application tracks: Organizations & Medical Students
# - Stores submissions in Supabase Postgres (JSONB payloads)
# - Uploads are saved to Supabase Storage with signed URLs
# - Admin tab to view & export CSV for scoring
# - Enforces 2025 deadlines: Orgs (Oct 17, 2025 @ 4:59 PM ET), Students (Oct 19, 2025 @ 11:59 PM ET)
#
# Requirements:
#   pip install streamlit pandas supabase
# Environment (Streamlit Cloud -> Secrets):
#   SUPABASE_URL
#   SUPABASE_ANON_KEY
#   SUPABASE_BUCKET = "nhcma-uploads"   (create in Supabase Storage)
#
# SQL (run in Supabase):
#   create table if not exists public.submissions (
#     id bigserial primary key,
#     track text not null check (track in ('organization','student')),
#     ts_utc timestamptz not null default now(),
#     applicant_name text not null,
#     email text,
#     phone text,
#     payload_json jsonb not null,
#     uploads_json jsonb not null
#   );
#   alter table public.submissions enable row level security;
#   -- Minimal policies for testing (tighten later):
#   create policy read_submissions_auth on public.submissions for select to authenticated using (true);
#   create policy insert_submissions_anon on public.submissions for insert to anon with check (true);

import os, json, textwrap
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, Tuple, Optional

import streamlit as st
import pandas as pd
from supabase import create_client, Client

# ----------------------------
# App Settings
# ----------------------------
APP_TITLE = "NHCMA Foundation — 2025 Public Health Innovation Grants"
TIMEZONE = "America/New_York"

# Deadlines (ET)
ORG_DEADLINE = datetime(2025, 10, 17, 16, 59, tzinfo=ZoneInfo(TIMEZONE))
STU_DEADLINE = datetime(2025, 10, 19, 23, 59, tzinfo=ZoneInfo(TIMEZONE))

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL") or st.secrets.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or st.secrets.get("SUPABASE_ANON_KEY")
BUCKET = os.getenv("SUPABASE_BUCKET") or st.secrets.get("SUPABASE_BUCKET", "nhcma-uploads")

@st.cache_resource(show_spinner=False)
def supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        st.stop()  # Fail fast with a helpful message
    return create_client(str(SUPABASE_URL), str(SUPABASE_ANON_KEY))

sb = supabase_client()

# ----------------------------
# Utilities
# ----------------------------
def too_late(deadline: datetime) -> bool:
    now = datetime.now(ZoneInfo(TIMEZONE))
    return now > deadline

def save_upload_to_storage(file, prefix: str) -> str:
    """
    Upload to Supabase Storage and return a signed URL (7 days).
    Bucket must exist and allow 'upload' by anon or use service function.
    """
    if file is None:
        return ""
    # Ensure a reasonable object key
    safe_name = file.name.replace("/", "_").replace("\\", "_")
    key = f"{prefix}/{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{safe_name}"
    # Upload
    try:
        sb.storage.from_(BUCKET).upload(key, file, file_options={"content-type": file.type})
    except Exception as e:
        st.warning(f"Upload failed for {safe_name}: {e}")
        return ""
    # Signed URL
    try:
        signed = sb.storage.from_(BUCKET).create_signed_url(key, expires_in=60*60*24*7)
        # The python client returns a dict; handle both shapes
        if isinstance(signed, dict):
            return signed.get("signedURL") or signed.get("signed_url") or ""
        return str(signed)
    except Exception as e:
        st.warning(f"Could not create signed URL for {safe_name}: {e}")
        # Fallback to public URL if bucket is public
        try:
            public_url = sb.storage.from_(BUCKET).get_public_url(key)
            return public_url
        except Exception:
            return ""

# ----------------------------
# DB helpers (Supabase)
# ----------------------------
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
        # Expand some common payload keys for quick view
        def g(p, key, default=""):
            try:
                return (p or {}).get(key, default)
            except Exception:
                return default
        flat = []
        for p in df.get("payload_json", []):
            flat.append({
                "Org Name": g(p, "org_name"),
                "Project Title": g(p, "project_title"),
                "School": g(p, "school"),
                "Advisor Name": g(p, "advisor_name"),
                "Budget Total": g(p, "budget_total"),
            })
        flat_df = pd.DataFrame(flat)
        df = pd.concat([df, flat_df], axis=1)
    return df

# ----------------------------
# Forms
# ----------------------------
def org_form() -> Tuple[bool, Dict[str, Any], Dict[str, str], str, str, str]:
    st.subheader("Organization Application (2025)")
    st.caption("Submission deadline: **October 17, 2025 at 4:59 PM ET**")

    disabled = too_late(ORG_DEADLINE)
    if disabled:
        st.error("The organization submission deadline has passed.")
    
    org_name = st.text_input("Name of Organization*", disabled=disabled)
    applicant_name = st.text_input("Name of Applicant (First/Last)*", disabled=disabled)
    email = st.text_input("Applicant Email*", disabled=disabled)
    phone = st.text_input("Applicant Phone*", disabled=disabled)

    exec_dir = st.text_input("Executive Director (First/Last)", disabled=disabled)
    exec_email = st.text_input("Executive Director Email", disabled=disabled)
    exec_phone = st.text_input("Executive Director Phone", disabled=disabled)

    mission = st.text_area("Organization Mission (brief)", disabled=disabled)

    st.markdown("**Eligibility (must confirm all):**")
    eligible_nonprofit = st.checkbox("Organization is a not-for-profit.", disabled=disabled)
    eligible_report = st.checkbox("Recipient will present final report at the NHCMA winter meeting in 2025 (date TBA).", disabled=disabled)
    eligible_benefit = st.checkbox("Funding will benefit residents of the Greater New Haven area.", disabled=disabled)

    st.markdown("**Introduction & Purpose (≈250 words each):**")
    q1 = st.text_area("1) Public health issue addressed in Greater New Haven", disabled=disabled)
    q2 = st.text_area("2) Alignment with NHCMA Foundation mission", disabled=disabled)
    q3 = st.text_area("3) Direct benefit to Greater New Haven residents", disabled=disabled)

    st.markdown("**Proposal Guidelines:**")
    project_title = st.text_input("Project Title*", disabled=disabled)
    desc = st.text_area("4) Detailed project description (objectives, methodology, expected outcomes)", disabled=disabled)
    budget = st.text_area("5) Itemized budget (include any outside funding)", disabled=disabled)
    budget_total = st.text_input("Budget total (USD)", disabled=disabled)
    timeline = st.text_area("6) Project timeline (goal within 1 year of disbursement)", disabled=disabled)
    evaluation = st.text_area("7) Evaluation plan (impact/outcomes in Greater New Haven)", disabled=disabled)

    st.markdown("**Attachments (PDF preferred):**")
    proposal_pdf = st.file_uploader("Upload Proposal / Narrative", type=["pdf","doc","docx"], disabled=disabled)
    budget_file  = st.file_uploader("Upload Budget", type=["pdf","xls","xlsx","csv"], disabled=disabled)
    other_file   = st.file_uploader("Optional: Additional Materials (letter(s) of support, etc.)", type=["pdf","doc","docx","zip"], disabled=disabled)

    submitted = st.button("Submit Organization Application", type="primary", disabled=disabled)

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
        # basic validations
        required = [org_name, applicant_name, email, project_title]
        if not all(x and str(x).strip() for x in required):
            st.warning("Please complete all required fields marked with * before submitting.")
            submitted = False
        elif not all([eligible_nonprofit, eligible_report, eligible_benefit]):
            st.warning("Please confirm all eligibility checkboxes.")
            submitted = False
        else:
            uploads["proposal"] = save_upload_to_storage(proposal_pdf, "org_proposal")
            uploads["budget"] = save_upload_to_storage(budget_file, "org_budget")
            uploads["other"] = save_upload_to_storage(other_file, "org_other")

    return submitted, payload, uploads, applicant_name, email, (phone or "")

def student_form() -> Tuple[bool, Dict[str, Any], Dict[str, str], str, str, str]:
    st.subheader("Medical Student Application (2025)")
    st.caption("Submission deadline: **October 19, 2025 at 11:59 PM ET**")

    disabled = too_late(STU_DEADLINE)
    if disabled:
        st.error("The student submission deadline has passed.")

    applicant_name = st.text_input("Applicant Name (First/Last)*", disabled=disabled)
    school = st.selectbox(
        "Medical School*",
        ["", "Frank H. Netter MD School of Medicine at Quinnipiac University", "Yale School of Medicine"],
        index=0,
        disabled=disabled
    )
    grad_date = st.text_input("Projected Graduation Date (MM/YYYY)", disabled=disabled)
    email = st.text_input("School Email*", disabled=disabled)
    phone = st.text_input("Phone*", disabled=disabled)

    advisor_name = st.text_input("Advisor Name", disabled=disabled)
    advisor_title = st.text_input("Advisor Title/Role", disabled=disabled)
    advisor_email = st.text_input("Advisor Email", disabled=disabled)

    st.markdown("**Eligibility (must confirm all):**")
    elig_enrolled = st.checkbox("I am currently enrolled at Quinnipiac (Netter) or Yale SOM.", disabled=disabled)
    elig_report = st.checkbox("If awarded, I will present results at the NHCMA winter meeting in 2025 (date TBA).", disabled=disabled)

    st.markdown("**Introduction & Purpose (≈250 words each):**")
    q1 = st.text_area("1) Public health issue addressed in Greater New Haven", disabled=disabled)
    q2 = st.text_area("2) Alignment with NHCMA Foundation mission", disabled=disabled)
    q3 = st.text_area("3) Direct benefit to Greater New Haven residents", disabled=disabled)

    st.markdown("**Proposal Guidelines:**")
    project_title = st.text_input("Project Title*", disabled=disabled)
    desc = st.text_area("4) Detailed project/research description (objectives, methodology, expected outcomes)", disabled=disabled)
    budget = st.text_area("5) Itemized budget (include any outside funding)", disabled=disabled)
    budget_total = st.text_input("Budget total (USD)", disabled=disabled)
    timeline = st.text_area("6) Timeline (goal within 1 year of disbursement)", disabled=disabled)
    evaluation = st.text_area("7) Evaluation plan (impact on public health in Greater New Haven)", disabled=disabled)

    st.markdown("**Attachments (PDF preferred):**")
    proposal_pdf = st.file_uploader("Upload Proposal / Narrative", type=["pdf","doc","docx"], disabled=disabled)
    budget_file  = st.file_uploader("Upload Budget", type=["pdf","xls","xlsx","csv"], disabled=disabled)
    cv_file      = st.file_uploader("Curriculum Vitae (PDF preferred)", type=["pdf","doc","docx"], disabled=disabled)
    support_let  = st.file_uploader("Letter of Support (optional)", type=["pdf","doc","docx"], disabled=disabled)

    submitted = st.button("Submit Student Application", type="primary", disabled=disabled)

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
            st.warning("Please complete all required fields marked with * before submitting.")
            submitted = False
        elif not all([elig_enrolled, elig_report]):
            st.warning("Please confirm all eligibility checkboxes.")
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
    # Simple CSV export
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV (All Submissions)",
        data=csv,
        file_name="nhcma_grants_submissions.csv",
        mime="text/csv"
    )

    # Scoring export (lite view)
    scoring_cols = ["id","track","ts_utc","applicant_name","email","phone","Org Name","School","Project Title","Budget Total"]
    export_df = df[[c for c in scoring_cols if c in df.columns]].copy()
    st.divider()
    st.caption("Scoring Export — key columns only")
    st.dataframe(export_df, use_container_width=True)
    csv2 = export_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV (Scoring Export)",
        data=csv2,
        file_name="nhcma_grants_scoring_export.csv",
        mime="text/csv"
    )

# ----------------------------
# Main
# ----------------------------
st.set_page_config(page_title="NHCMA Grants 2025", layout="wide")

st.title(APP_TITLE)
st.write("**Grant Amount:** Up to $2,500 • **Submission Year:** 2025")
st.info("Projects should benefit residents of the Greater New Haven area and be completed within one year of funding disbursement.")

tab1, tab2, tab3 = st.tabs(["Apply — Organizations", "Apply — Medical Students", "Admin"])

with tab1:
    submitted, payload, uploads, name, email, phone = org_form()
    if submitted:
        rid = insert_submission("organization", name, email, phone, payload, uploads)
        if rid:
            st.success("Thank you! Your organization application has been submitted.")
        else:
            st.error("There was a problem saving your submission. Please try again or contact support.")

with tab2:
    submitted, payload, uploads, name, email, phone = student_form()
    if submitted:
        rid = insert_submission("student", name, email, phone, payload, uploads)
        if rid:
            st.success("Thank you! Your student application has been submitted.")
        else:
            st.error("There was a problem saving your submission. Please try again or contact support.")

with tab3:
    admin_panel()

st.caption("© 2025 New Haven County Medical Association Foundation")
