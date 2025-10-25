
from __future__ import annotations

import io
import re
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# 3rd party
from supabase import create_client, Client
from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT

# Optional (only used for return report)
try:
    import pandas as pd  # noqa: F401
    _HAS_PANDAS = True
except Exception:  # pragma: no cover
    _HAS_PANDAS = False

# ----------------------------
# Config
# ----------------------------
# Storage bucket where booklet files are written
BUCKET_NAME = "nhcma-booklets"

# Source table of submissions/applications.
# You can override via environment or Streamlit secrets by importing this module
# and setting nhcma_booklet_builder.TABLE_NAME before calling build.
TABLE_NAME = "submissions"

# Known attachment keys we expect inside a JSON column or individual columns
ATTACHMENT_KEYS = ["proposal", "budget", "other", "cv", "support_letter"]

# A likely column name that contains a JSON blob of attachment paths or signed URLs
ATTACHMENTS_JSON_CANDIDATES = ["attachments", "attachment_urls", "files_json"]

# Column to persist the uploaded booklet path back to the record (if present)
BOOKLET_PATH_COLUMN = "booklet_docx_path"


# ----------------------------
# Supabase helpers
# ----------------------------
def get_supabase(url: str, key: str) -> Client:
    """Create a Supabase client from URL and service/anon key."""
    return create_client(url, key)


def _sb_storage_create_signed_url(sb: Client, bucket: str, path: str, expires_in_seconds: int) -> Optional[str]:
    try:
        res = sb.storage.from_(bucket).create_signed_url(path, expires_in_seconds)
        # Some SDKs return dict with 'signedURL', others with 'signed_url'
        return res.get("signedURL") or res.get("signed_url")
    except Exception:
        return None


def make_signed_url(sb: Client, bucket: str, path: str, expires_in_seconds: int = 30 * 24 * 3600) -> Optional[str]:
    """
    Create a time-limited URL for a storage object.
    Default: 30 days to match judging window.
    """
    return _sb_storage_create_signed_url(sb, bucket, path, expires_in_seconds)


# ----------------------------
# DOCX helpers
# ----------------------------
def _add_hyperlink(paragraph, text: str, url: str):
    """
    Insert a clickable hyperlink using a field code:
      <w:fldSimple w:instr='HYPERLINK "url"'>
        <w:r><w:rPr>style</w:rPr><w:t>text</w:t></w:r>
      </w:fldSimple>
    This renders as a blue, underlined clickable link in Word.
    """
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), f'HYPERLINK "{url}"')

    r = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")

    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rPr.append(u)

    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0000FF")
    rPr.append(color)

    r.append(rPr)

    t = OxmlElement("w:t")
    t.text = text
    r.append(t)

    fld.append(r)
    paragraph._p.append(fld)
    return paragraph


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "booklet"


def _get_first_nonempty(row: Dict[str, Any], candidates: List[str]) -> Optional[str]:
    for c in candidates:
        v = row.get(c)
        if v:
            return str(v)
    return None

def _attachments_from_row(row: Dict[str, Any]) -> List[Tuple[str, Optional[str]]]:
    """
    Extract a list of (label, url_or_path) from a row.
    Preference:
      1) JSON blob if present AND has at least one non-empty value
      2) Otherwise, fall back to individual columns (case-insensitive, underscore/space tolerant)
    """
    # Normalize keys for tolerant lookups
    norm_map = {}
    for k, v in row.items():
        if k is None:
            continue
        norm = re.sub(r"[\s_]+", "_", str(k).strip().lower())
        norm_map[norm] = v

    # --- 1) Try JSON-style attachments ---
    json_data = None
    for cand in ATTACHMENTS_JSON_CANDIDATES:
        blob = row.get(cand)
        if not blob:
            continue
        try:
            json_data = blob if isinstance(blob, dict) else json.loads(str(blob))
            if not isinstance(json_data, dict):
                json_data = None
        except Exception:
            json_data = None

        if isinstance(json_data, dict):
            collected = []
            any_nonempty = False
            for key in ATTACHMENT_KEYS:
                # allow flexible keys inside JSON too (e.g., "support letter" vs "support_letter")
                variants = {
                    key,
                    key.replace("_", " "),
                    key.replace("_", "").lower(),
                }
                value = None
                for var in variants:
                    if var in json_data:
                        value = json_data.get(var)
                        break
                collected.append((key, value))
                if value:
                    any_nonempty = True

            if any_nonempty:
                return collected
            # else: fall through to per-column scan

    # --- 2) Per-column fallback (tolerant to spaces/underscores/casing) ---
    out = []
    for key in ATTACHMENT_KEYS:
        variants = [
            key,
            key.replace("_", " "),
            key.replace("_", ""),  # e.g., "supportletter"
        ]
        if key == "cv":
            variants += ["curriculum_vitae"]

        found = None
        for var in variants:
            norm = re.sub(r"[\s_]+", "_", var.strip().lower())
            if norm in norm_map and norm_map[norm]:
                found = norm_map[norm]
                break

        out.append((key, found))
    return out


def _humanize_label(label: str) -> str:
    return label.replace("_", " ").title()


def build_booklet_docx(sb: Client, row: Dict[str, Any]) -> bytes:
    """
    Build a single booklet (DOCX) for one submission row.
    Returns the file bytes.
    """
    doc = Document()

    # --- Title ---
    title = _get_first_nonempty(row, ["project_title", "title", "application_title", "proposal_title"]) or "Application"
    org = _get_first_nonempty(row, ["organization_name", "org_name", "applicant_organization", "submitter"]) or ""
    p = doc.add_paragraph()
    run = p.add_run(title)
    run.bold = True
    run.font.size = Pt(16)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if org:
        p2 = doc.add_paragraph(org)
        p2.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_paragraph("")

    # --- Basic fields that are common; we only render those that exist ---
    basic_fields = [
        ("Applicant", ["applicant_name", "contact_name", "full_name"]),
        ("Email", ["applicant_email", "email"]),
        ("Phone", ["applicant_phone", "phone"]),
        ("Requested Amount", ["requested_amount", "amount_requested"]),
        ("Summary", ["summary", "project_summary", "abstract"]),
    ]
    for label, candidates in basic_fields:
        val = _get_first_nonempty(row, candidates)
        if val:
            para = doc.add_paragraph()
            r = para.add_run(f"{label}: ")
            r.bold = True
            para.add_run(str(val))

    # --- Attachments ---
    doc.add_paragraph("")
    hdr = doc.add_paragraph()
    r = hdr.add_run("Attachments")
    r.bold = True

    attachments = _attachments_from_row(row)
    if not attachments:
        para = doc.add_paragraph("— None —")
    else:
        for key, raw in attachments:
            para = doc.add_paragraph()
            r = para.add_run(f"{_humanize_label(key)}: ")
            r.bold = True
            if not raw:
                para.add_run("—")
                continue

            # If it's a storage path, attempt to sign it; if it's already an http(s) URL, use as-is
            url = str(raw)
            if not re.match(r"^https?://", url, flags=re.I):
                # Heuristic: treat as Storage path
                signed = make_signed_url(sb, BUCKET_NAME, url, 30 * 24 * 3600)
                url = signed or url  # fall back to raw path

            # Insert clickable hyperlink
            _add_hyperlink(para, url, url)

    # Footer
    doc.add_paragraph("")
    small = doc.add_paragraph()
    rr = small.add_run(f"Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} • NHCMA Judging Booklet")
    rr.font.size = Pt(8)

    # Serialize
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _upload_bytes(sb: Client, bucket: str, path: str, data: bytes, content_type: str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document") -> str:
    """
    Upload bytes to Supabase Storage (upsert). Returns the object path.
    """
    sb.storage.from_(bucket).upload(path, data, {"content-type": content_type, "upsert": "true"})
    return path


def _safe(str_or_none: Optional[str]) -> str:
    return (str_or_none or "").strip()


def _row_id(row: Dict[str, Any]) -> Optional[str]:
    for k in ("id", "application_id", "submission_id"):
        v = row.get(k)
        if v is not None:
            return str(v)
    return None


def list_submissions(sb: Client, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    q = sb.table(TABLE_NAME).select("*")
    if where:
        for k, v in where.items():
            q = q.eq(k, v)
    res = q.execute()
    data = getattr(res, "data", None) or getattr(res, "json", None) or []
    # supabase-py returns dict with 'data'
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    return data or []


def build_all_booklets(sb: Client, where: Optional[Dict[str, Any]] = None) -> "pd.DataFrame | List[Dict[str, Any]]":
    """
    Build DOCX booklets for all rows matching the filter, upload to Storage,
    and (if possible) persist booklet path back to the row.
    Returns a DataFrame (if pandas installed) or a list of dicts.
    """
    rows = list_submissions(sb, where=where)
    out: List[Dict[str, Any]] = []

    for row in rows:
        try:
            rid = _row_id(row) or "unknown"
            org = _get_first_nonempty(row, ["organization_name", "org_name"]) or "org"
            title = _get_first_nonempty(row, ["project_title", "title"]) or "application"

            filename = f"{_slugify(org)}--{_slugify(title)}--{rid}.docx"
            path = f"booklets/{rid}/{filename}"

            data = build_booklet_docx(sb, row)
            uploaded_path = _upload_bytes(sb, BUCKET_NAME, path, data)

            # Persist path back if column exists (best-effort)
            try:
                if BOOKLET_PATH_COLUMN in row:
                    sb.table(TABLE_NAME).update({BOOKLET_PATH_COLUMN: uploaded_path}).eq("id", row.get("id")).execute()
            except Exception:
                pass

            out.append({
                "id": rid,
                "title": title,
                "organization": org,
                "booklet_docx_path": uploaded_path,
                "signed_url_30d": make_signed_url(sb, BUCKET_NAME, uploaded_path, 30 * 24 * 3600),
            })
        except Exception as e:
            out.append({
                "id": _row_id(row) or "unknown",
                "error": str(e),
            })

    if _HAS_PANDAS:
        import pandas as pd  # local import
        return pd.DataFrame(out)
    return out
