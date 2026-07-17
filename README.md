NHCMA Foundation Grants App — README
Overview

This Streamlit app manages grant submissions for the NHCMA Foundation.
It supports:

Two submission tracks: Organizations and Medical Students

File uploads (proposal, budget, other materials) to Supabase Storage

Automatic email confirmations and notifications via Microsoft Graph (application auth)

Admin portal for reviewing, exporting (CSV/XLSX), and downloading submissions

App Email Architecture
NHCMA Grants app (NHCMA_Grants_App_Supabase.py): Microsoft Graph
Other apps in this repo may still use SMTP (for example, the Posters app). Keep SMTP documentation separate from Grants settings.

Grants App Secrets Configuration (Streamlit Cloud -> Settings -> Secrets)
# Supabase
SUPABASE_URL = "https://<your-project>.supabase.co"
SUPABASE_ANON_KEY = "<anon key>"
SUPABASE_BUCKET = "nhcma-uploads"
SUPABASE_SERVICE_ROLE_KEY = "<service role key>"   # required for admin & inserts

# Admin
ADMIN_PASSWORD = "<your chosen admin password>"

# Grants email (Microsoft Graph)
MS_TENANT_ID = "..."
MS_CLIENT_ID = "..."
MS_CLIENT_SECRET = "..."
MS_SENDER_EMAIL = "foundation@nhcma.org"
ADMIN_NOTIFICATION_EMAIL = "office@nhcma.org"

Notes
- MS_SENDER_EMAIL must be a mailbox that your Entra app registration is authorized to send as (Mail.Send application permission plus mailbox/send-as authorization as required by your tenant policy).
- ADMIN_NOTIFICATION_EMAIL receives administrative copy emails and is also used for applicant-facing contact references in grants templates.

SMTP Configuration for Other Apps
SMTP settings are not used by the Grants app anymore. If another app (such as the Posters app) still relies on SMTP, document and manage those SMTP settings in that app's README.

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

Administrative copy: ADMIN_NOTIFICATION_EMAIL (default office@nhcma.org)

Sent from: MS_SENDER_EMAIL via Microsoft Graph application authentication

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