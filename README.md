NHCMA Foundation Grants App — README
Overview

This Streamlit app manages grant submissions for the NHCMA Foundation.
It supports:

Two submission tracks: Organizations and Medical Students

File uploads (proposal, budget, other materials) to Supabase Storage

Automatic email confirmations (Office365 SMTP)

Admin portal for reviewing, exporting (CSV/XLSX), and downloading submissions

Secrets Configuration (Streamlit Cloud → Settings → Secrets)
# Supabase
SUPABASE_URL = "https://<your-project>.supabase.co"
SUPABASE_ANON_KEY = "<anon key>"
SUPABASE_BUCKET = "nhcma-uploads"
SUPABASE_SERVICE_ROLE_KEY = "<service role key>"   # required for admin & inserts

# Admin
ADMIN_PASSWORD = "<your chosen admin password>"

# SMTP (Office365)
[smtp]
host = "smtp.office365.com"
port = 587
user = "ray@lutinemanagement.com"
password = "<Office365 app password>"
from_addr = "ray@lutinemanagement.com"
from_name = "NHCMA Foundation Grants"

Deadlines (built into app)

Organization Applications: October 17, 2025 @ 4:59 PM ET

Student Applications: October 19, 2025 @ 11:59 PM ET

App Structure

Apply — Organizations
Form + file upload; writes to submissions table.

Apply — Medical Students
Form + file upload; writes to submissions table.

Admin (password protected)

View submissions (with clickable proposal/budget/other links)

Export CSV (universal)

Export XLSX (clickable links, Excel native)

Scoring export with key fields only

Emails

To applicant: confirmation email with submission details

CC: nhcma@lutinemanagement.com

Sent from: ray@lutinemanagement.com (Office365 via SMTP)

Data Storage

Database:

Table public.submissions

Columns: id, applicant info, payload_json, uploads_json

Storage:

Bucket nhcma-uploads

Folders: org_proposal, org_budget, org_other, etc.

Files saved with timestamp prefixes to avoid collisions

Resetting for New Cycle
Clear Submissions (database)
truncate table public.submissions restart identity cascade;

Clear Files (storage)

Option A: Supabase Dashboard → Storage → nhcma-uploads → select all → Delete

Option B: Run a cleanup script with the service-role key

Dependencies

Add these to requirements.txt:

streamlit
supabase-py
pandas
openpyxl>=3.1

Known Notes

Submitters must complete the application in one sitting (no partial saves).

Admin portal is password-protected (see ADMIN_PASSWORD).

RLS is enabled; all public inserts/reads use the service-role key.

XLSX exports provide reviewers with immediately clickable file links.

✦ This document is part of the Doomsday Compendium.
It provides everything needed to redeploy or recover the NHCMA Foundation Grants app.
