# NHCMA — Research Poster Submissions App

Streamlit app for collecting **Student, Resident, and Fellow** research poster submissions.  
Runs alongside the NHCMA Foundation Grants app, using the same Supabase project.

---

## Overview

- Collects:
  - Category: Student / Resident / Fellow  
  - Lead Author name & institution  
  - Contact email (required)  
  - Up to 3 co-authors + institutions  
  - Title of project (required)  
  - Abstract (required, ≤250 words)  
  - Optional PDF upload of the poster  

- Stores submissions in **Supabase table `public.posters`**  
- Files stored privately in **bucket `nhcma-posters`** (signed URLs)  
- Sends email confirmation to submitter, CC to **nhcma@lutinemanagement.com**  
- Admin panel (password-protected) allows viewing/export of all submissions  

---

## Supabase Setup

**Table:**

```sql
create table public.posters (
    id bigserial primary key,
    created_at timestamptz default now(),
    category text not null check (category in ('Student','Resident','Fellow')),
    lead_author text not null,
    coauthor1 text,
    coauthor2 text,
    coauthor3 text,
    institution_lead text,
    institution_co1 text,
    institution_co2 text,
    institution_co3 text,
    title text not null,
    abstract text not null,
    poster_url text,
    contact_email text not null
);
Bucket:

nhcma-posters (private)

Policies:

Insert allowed for anon (open submission)

Read allowed for service_role (used in admin panel)

Streamlit Secrets
Example secrets.toml:

toml
Copy code
SUPABASE_URL = "https://<yourref>.supabase.co"
SUPABASE_ANON_KEY = "<anon>"
SUPABASE_SERVICE_ROLE_KEY = "<service role>"
SUPABASE_BUCKET = "nhcma-posters"

[smtp]
host = "smtp.office365.com"
port = 587
user = "ray@lutinemanagement.com"
password = "<Office365 app password>"
from_addr = "ray@lutinemanagement.com"
from_name = "NHCMA Foundation"

ADMIN_PASSWORD = "********"
Admin Panel
Protected by ADMIN_PASSWORD in secrets

Login required to view submissions

Exports available as CSV (clickable links preserved)

Deployment
Repo: NHCMA_External_Apps

Main file: NHCMA_Posters_App.py (root level, not inside pages/)

Streamlit Cloud: separate app from Grants

Requirements: streamlit, supabase-py, pandas, openpyxl

Notes
Abstract length is validated (≤250 words)

Duplicate submissions prevented with session token check

Signed URLs for uploaded PDFs expire after 7 days

Manual bucket cleanup may be needed if test files were uploaded

Poster app runs independently but uses the same Supabase project as the Grants app
