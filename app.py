"""SY Communications · Quick quote survey (customer-facing).

A branded 2-minute form customers open from the survey email sent by Prospect Engine / Lead Revival.
On submit it:
  1. saves the answers onto the customer's Zoho lead (or finds / creates one), adds a note and a tag,
  2. emails the customer a branded copy with a PDF of their answers (sent from Zoho, logged on the lead),
  3. optionally alerts you, and
  4. records the response in the shared GitHub data repo, so the other apps and Lead Overview can count it.

Links from the apps are signed (SURVEY_SIGNING_KEY), so nobody can point a submission at someone else's lead.
Admin page (status checks, Zoho key helper, test links): add ?admin=1 to the address.
"""
import base64
import hashlib
import hmac
import html as html_lib
import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import streamlit.components.v1 as components
from fpdf import FPDF

UK = ZoneInfo("Europe/London")
PURPLE, PURPLE_2, TEAL = "#1f1450", "#2d1f6e", "#00b5a3"

st.set_page_config(page_title="Your phone system quote · SY Communications", page_icon="📞",
                   layout="centered", initial_sidebar_state="collapsed")


# =====================================================================
# Settings
# =====================================================================
def secret(key: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(key, default) or default)
    except Exception:
        return default


COMPANY = secret("COMPANY_NAME", "SY Communications")
CONTACT = {
    "phone": secret("SENDER_PHONE", "01743 667419"),
    "email": secret("SENDER_EMAIL", "hello@sycomms.co.uk"),
    "website": secret("SENDER_WEBSITE", "www.sycomms.co.uk"),
    "address": secret("SENDER_ADDRESS", "Suite C, Jupiter House, Shrewsbury SY2 6LG"),
    "name": secret("SENDER_NAME", ""),
    "title": secret("SENDER_TITLE", ""),
}
SURVEY_FROM = secret("SURVEY_FROM", CONTACT["email"]).strip().lower()
SURVEY_ALERT_TO = secret("SURVEY_ALERT_TO").strip()
SURVEY_TAG_DONE = secret("SURVEY_DONE_TAG", "Survey completed")
SURVEY_SOURCE = secret("SURVEY_LEAD_SOURCE", "Survey form")
BOOKING_URL = secret("DEMO_BOOKING_URL")
SIGNING_KEY = secret("SURVEY_SIGNING_KEY")
ZOHO_SCOPE = ("ZohoCRM.modules.leads.ALL,ZohoCRM.modules.notes.CREATE,ZohoCRM.coql.READ,ZohoCRM.settings.fields.READ,"
              "ZohoCRM.send_mail.leads.CREATE,ZohoCRM.Files.CREATE,ZohoCRM.settings.emails.READ")
APP_NAMES = {"pe": "Prospect Engine", "lr": "Lead Revival", "mp": "MY PA", "cg": "Customer Growth"}

CALL_OPTIONS = ["Desk phones", "Softphone on a computer", "Mobile app", "Not sure yet"]
NUMBER_OPTIONS = ["Keep our existing number(s)", "New number(s)", "A mix of both"]
AGE_OPTIONS = ["Under 2 years", "2–5 years", "5–10 years", "10+ years", "Not sure"]


def sign(lead_id: str, email: str) -> str:
    """Same recipe as Prospect Engine / Lead Revival use when they build the link."""
    msg = f"{lead_id}|{(email or '').strip().lower()}".encode()
    return hmac.new(SIGNING_KEY.encode(), msg, hashlib.sha256).hexdigest()[:20]


def signed_link(base: str, lead_id: str, company: str, name: str, email: str, phone: str = "", src: str = "") -> str:
    q = {"lid": lead_id, "c": company, "n": name, "e": email, "p": phone, "src": src}
    q = {k: v for k, v in q.items() if v}
    if SIGNING_KEY and lead_id:
        q["s"] = sign(lead_id, email)
    return base + ("&" if "?" in base else "?") + urlencode(q)


def now_uk() -> datetime:
    return datetime.now(UK)


def esc(v: Any) -> str:
    return html_lib.escape(str(v if v is not None else ""))


def norm(t: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(t or "").lower())


# =====================================================================
# Zoho CRM (own Self Client refresh token, EU data centre by default)
# =====================================================================
class ZohoError(RuntimeError):
    pass


@st.cache_resource
def _token_box() -> Dict[str, Any]:
    return {}


class Zoho:
    def __init__(self) -> None:
        self.client_id = secret("ZOHO_CLIENT_ID")
        self.client_secret = secret("ZOHO_CLIENT_SECRET")
        self.refresh_token = secret("ZOHO_REFRESH_TOKEN")
        self.accounts = secret("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.eu").rstrip("/")
        self.api = secret("ZOHO_API_DOMAIN", "https://www.zohoapis.eu").rstrip("/")
        self.crm_url = secret("ZOHO_CRM_URL", "https://crm.zoho.eu").rstrip("/")
        self.configured = bool(self.client_id and self.client_secret and self.refresh_token)

    def exchange_code(self, code: str) -> Dict[str, str]:
        try:
            data = requests.post(f"{self.accounts}/oauth/v2/token", timeout=15, data={
                "grant_type": "authorization_code", "client_id": self.client_id,
                "client_secret": self.client_secret, "code": code.strip()}).json()
        except Exception as exc:
            return {"error": f"Couldn't reach Zoho ({exc.__class__.__name__})."}
        if data.get("refresh_token"):
            return {"refresh_token": data["refresh_token"]}
        return {"error": {"invalid_code": "That code has expired or was already used. Generate a new one and paste it"
                                          " straight away.",
                          "invalid_client": "ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET don't match the Self Client."}.get(
            data.get("error"), f"Zoho said: {data.get('error') or 'no refresh token returned'}")}

    def _token(self, force: bool = False) -> str:
        box = _token_box()
        if not force and box.get("t") and box.get("exp", 0) > time.time() + 60:
            return box["t"]
        try:
            data = requests.post(f"{self.accounts}/oauth/v2/token", timeout=12, params={
                "refresh_token": self.refresh_token, "client_id": self.client_id,
                "client_secret": self.client_secret, "grant_type": "refresh_token"}).json()
        except Exception as exc:
            raise ZohoError(f"Couldn't reach Zoho to sign in ({exc.__class__.__name__}).")
        if "access_token" not in data:
            raise ZohoError(f"Zoho sign-in failed: {data.get('error', 'unknown error')}")
        box.update(t=data["access_token"], exp=time.time() + int(data.get("expires_in", 3600)))
        return box["t"]

    def request(self, method: str, path: str, **kw) -> Optional[Dict[str, Any]]:
        for attempt in (0, 1):
            headers = {"Authorization": f"Zoho-oauthtoken {self._token(force=attempt == 1)}"}
            try:
                r = requests.request(method, f"{self.api}{path}", headers=headers, timeout=25, **kw)
            except requests.exceptions.RequestException as exc:
                raise ZohoError(f"Couldn't reach Zoho CRM ({exc.__class__.__name__}).")
            if r.status_code == 204:
                return None
            try:
                body = r.json()
            except ValueError:
                body = {}
            code = str(body.get("code", ""))
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code >= 400:
                row = (body.get("data") or [{}])[0] if isinstance(body.get("data"), list) else {}
                raise ZohoError(f"Zoho CRM error: {body.get('message') or row.get('message') or code or r.status_code}")
            return body
        raise ZohoError("Zoho CRM rejected the sign-in.")

    @staticmethod
    def row(body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        r = ((body or {}).get("data") or [{}])[0]
        if r.get("status") != "success":
            raise ZohoError(f"Zoho CRM error: {r.get('message') or r.get('code') or 'unknown'}")
        return r.get("details") or {}

    # --- reads
    def lead(self, lead_id: str) -> Optional[Dict[str, Any]]:
        try:
            body = self.request("GET", f"/crm/v8/Leads/{lead_id}",
                                params={"fields": "Company,First_Name,Last_Name,Email,Phone,Street,City,Zip_Code,Lead_Status"})
        except ZohoError:
            return None  # Deleted, converted or not ours
        return ((body or {}).get("data") or [None])[0]

    def find_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        if not email:
            return None
        try:
            body = self.request("GET", "/crm/v8/Leads/search", params={"email": email})
        except ZohoError:
            return None
        rows = (body or {}).get("data") or []
        return sorted(rows, key=lambda r: str(r.get("Modified_Time") or ""), reverse=True)[0] if rows else None

    def from_addresses(self) -> List[Dict[str, Any]]:
        body = self.request("GET", "/crm/v8/settings/emails/actions/from_addresses") or {}
        return [a for a in body.get("from_addresses", []) if a.get("email")]

    # --- writes
    def update_lead(self, lead_id: str, fields: Dict[str, Any]) -> None:
        if fields:
            self.row(self.request("PUT", "/crm/v8/Leads",
                                  json={"data": [dict(fields, id=lead_id)], "trigger": ["workflow"]}))

    def create_lead(self, fields: Dict[str, Any]) -> str:
        # No workflow trigger: keeps any old webform rules from acting on these
        return str(self.row(self.request("POST", "/crm/v8/Leads", json={"data": [fields], "trigger": []})).get("id"))

    def add_note(self, lead_id: str, title: str, content: str) -> None:
        note = {"Note_Title": title, "Note_Content": content,
                "Parent_Id": {"module": {"api_name": "Leads"}, "id": lead_id}}
        self.row(self.request("POST", f"/crm/v8/Leads/{lead_id}/Notes", json={"data": [note]}))

    def add_tag(self, lead_id: str, tag: str) -> None:
        self.request("POST", f"/crm/v8/Leads/{lead_id}/actions/add_tags",
                     json={"tags": [{"name": tag}], "over_write": False})

    def upload_file(self, filename: str, data: bytes) -> str:
        return self.row(self.request("POST", "/crm/v8/files",
                                     files={"file": (filename, data, "application/pdf")}))["id"]

    def send_mail(self, lead_id: str, sender: Dict[str, Any], to_email: str, to_name: str, subject: str, html: str,
                  attachment_ids: Optional[List[str]] = None) -> None:
        mail: Dict[str, Any] = {"from": {"user_name": sender.get("user_name") or COMPANY, "email": sender["email"]},
                                "to": [{"user_name": to_name or "", "email": to_email}],
                                "subject": subject, "content": html, "mail_format": "html"}
        if sender.get("type") == "org_email":
            mail["org_email"] = True
        if attachment_ids:
            mail["attachments"] = [{"id": a} for a in attachment_ids]
        self.row(self.request("POST", f"/crm/v8/Leads/{lead_id}/actions/send_mail", json={"data": [mail]}))

    def record_url(self, lead_id: str) -> str:
        org = secret("ZOHO_ORG").strip("/")
        return f"{self.crm_url}/crm/{org}/tab/Leads/{lead_id}" if org else f"{self.crm_url}/crm/tab/Leads/{lead_id}"


ZOHO = Zoho()


@st.cache_data(ttl=3600, show_spinner=False)
def lead_fields() -> Dict[str, Dict[str, Any]]:
    """Lead field definitions (api name → definition). Cached for an hour."""
    body = ZOHO.request("GET", "/crm/v8/settings/fields", params={"module": "Leads"}) or {}
    return {f["api_name"]: f for f in body.get("fields", [])}


@st.cache_data(ttl=3600, show_spinner=False)
def sender_address() -> Optional[Dict[str, Any]]:
    addrs = ZOHO.from_addresses()
    pick = next((a for a in addrs if (a.get("email") or "").lower() == SURVEY_FROM), None)
    return pick or (addrs[0] if addrs else None)


# Survey answer → the Lead field that holds it. Matched on the field's label or API name, any spelling/case,
# so this keeps working whatever the fields were called when they were made.
FIELD_MAP = [
    ("done", ("surveycompleted",)),
    ("users", ("noofusers", "numberofusers", "users")),
    ("handsets", ("handsetsrequired", "noofhandsets", "numberofhandsets", "handsets")),
    ("call_pref", ("callpreference", "devicepreference", "howtotakecalls")),
    ("sites", ("numberofsites", "noofsites", "sites")),
    ("other_sites", ("othersites", "othersiteaddresses")),
    ("numbers", ("phonenumbers", "numberchoice")),
    ("port_numbers", ("numberstoport", "numberstokeep")),
    ("system", ("existingsystem", "currentsystem", "existingsystemmakemodel")),
    ("system_age", ("existingsystemage", "systemage", "currentsystemage")),
]
KEYWORDS = {
    "Desk phones": ("desk",), "Softphone on a computer": ("soft", "computer"), "Mobile app": ("mobile", "app"),
    "Not sure yet": ("notsure", "sure"), "Keep our existing number(s)": ("port", "keep", "existing"),
    "New number(s)": ("new",), "A mix of both": ("both", "mix"), "Under 2 years": ("under2", "lessthan2", "02"),
    "2–5 years": ("25",), "5–10 years": ("510",), "10+ years": ("10", "over10"), "Not sure": ("notsure", "sure"),
}


def match_option(field: Dict[str, Any], value: str) -> Optional[str]:
    opts = [o.get("actual_value") or o.get("display_value") for o in field.get("pick_list_values") or []]
    opts = [o for o in opts if o and o != "-None-"]
    for o in opts:
        if norm(o) == norm(value):
            return o
    for kw in KEYWORDS.get(value, ()):
        for o in opts:
            if kw in norm(o):
                return o
    return None


def survey_field_values(ans: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """({api name: value}, [answers that had no matching field]) for the survey answers."""
    try:
        fields = lead_fields()
    except ZohoError:
        return {}, [k for k, _ in FIELD_MAP]
    by_norm: Dict[str, Dict[str, Any]] = {}
    for api, f in fields.items():
        by_norm.setdefault(norm(f.get("field_label")), f)
        by_norm.setdefault(norm(api), f)
    out, missing = {}, []
    for key, aliases in FIELD_MAP:
        f = next((by_norm[a] for a in aliases if a in by_norm), None)
        val = True if key == "done" else ans.get(key)
        if val in (None, "", []):
            continue
        if not f:
            missing.append(key)
            continue
        dt = f.get("data_type")
        if dt == "boolean":
            out[f["api_name"]] = bool(val)
        elif dt in ("integer", "bigint", "double", "currency"):
            try:
                out[f["api_name"]] = int(val)
            except (TypeError, ValueError):
                pass
        elif dt == "picklist":
            pick = match_option(f, val if isinstance(val, str) else ", ".join(val))
            if pick:
                out[f["api_name"]] = pick
        elif dt == "multiselectpicklist":
            vals = [match_option(f, v) for v in (val if isinstance(val, list) else [val])]
            vals = [v for v in vals if v]
            if vals:
                out[f["api_name"]] = vals
        else:
            text = ", ".join(val) if isinstance(val, list) else str(val)
            limit = int(f.get("length") or 0)
            out[f["api_name"]] = text[:limit] if limit and dt == "text" else text
    return out, missing


# =====================================================================
# Shared data repo (GitHub): every response, for Prospect Engine / Lead Revival / Lead Overview stats
# =====================================================================
def gh_settings() -> Tuple[str, str, str, str]:
    return (secret("GITHUB_TOKEN"), secret("GITHUB_REPO"), secret("GITHUB_BRANCH", "main"),
            secret("GITHUB_SURVEY_PATH", "survey_responses.json"))


def gh_read() -> Tuple[Dict[str, Any], Optional[str]]:
    token, repo, branch, path = gh_settings()
    if not (token and repo):
        return {}, None
    r = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", params={"ref": branch}, timeout=15,
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    body = r.json()
    text = base64.b64decode(body.get("content") or b"").decode("utf-8-sig").strip()
    return (json.loads(text) if text else {}), body.get("sha")


def gh_save(key: str, record: Dict[str, Any]) -> Optional[str]:
    """Adds one response. Re-reads before writing, retries if someone saved at the same moment. Returns an error."""
    token, repo, branch, path = gh_settings()
    if not (token and repo):
        return "GITHUB_TOKEN / GITHUB_REPO not set"
    last = "GitHub kept rejecting the save"
    for _ in range(4):
        try:
            data, sha = gh_read()
            data[key] = record
            payload = {"message": f"Survey: {record.get('company') or 'response'}", "branch": branch,
                       "content": base64.b64encode(json.dumps(data, indent=1, ensure_ascii=False).encode()).decode()}
            if sha:
                payload["sha"] = sha
            r = requests.put(f"https://api.github.com/repos/{repo}/contents/{path}", json=payload, timeout=20,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            if r.status_code in (200, 201):
                return None
            last = f"GitHub error {r.status_code}"
            if r.status_code not in (409, 422):
                return last
        except Exception as exc:
            last = f"Couldn't save ({exc.__class__.__name__})"
        time.sleep(1)
    return last


# =====================================================================
# Answers → text, PDF and emails
# =====================================================================
def answer_rows(a: Dict[str, Any]) -> List[Tuple[str, List[Tuple[str, str]]]]:
    """Sections of (label, value) for the summary page, note, email and PDF."""
    addr = ", ".join(x for x in (a.get("street"), a.get("town"), a.get("postcode")) if x)
    sites = [("Main address", addr), ("Number of sites", str(a.get("sites") or 1))]
    if a.get("other_sites"):
        sites.append(("Other sites", a["other_sites"]))
    team = [("People taking calls", str(a.get("users") or "")),
            ("How they'll take calls", ", ".join(a.get("call_pref") or []) or "Not said")]
    if a.get("handsets"):
        team.append(("Desk handsets", str(a["handsets"])))
    nums = [("Phone numbers", a.get("numbers") or "Not said")]
    if a.get("port_numbers"):
        nums.append(("Numbers to keep", a["port_numbers"]))
    cur = [("Make and model", a.get("system") or "Not said"), ("Age", a.get("system_age") or "Not said")]
    out = [("Your details", [("Company", a.get("company", "")), ("Name", a.get("name", "")),
                             ("Email", a.get("email", "")), ("Phone", a.get("phone", ""))]),
           ("Your sites", sites), ("Your team", team), ("Phone numbers", nums), ("Current phone system", cur)]
    if a.get("notes"):
        out.append(("Anything else", [("Notes", a["notes"])]))
    return out


def answers_text(a: Dict[str, Any]) -> str:
    lines = []
    for title, rows in answer_rows(a):
        lines.append(title.upper())
        lines += [f"{k}: {v}" for k, v in rows]
        lines.append("")
    return "\n".join(lines).strip()


# ---------- PDF ----------
def _font_dir() -> Optional[str]:
    try:
        from inter_font import INTER_B64  # Bundled next to app.py
    except Exception:
        return None
    folder = os.path.join(tempfile.gettempdir(), "quote_survey_fonts")
    os.makedirs(folder, exist_ok=True)
    for w in (400, 600, 800):
        p = os.path.join(folder, f"inter{w}.ttf")
        if not os.path.exists(p):
            with open(p, "wb") as fh:
                fh.write(base64.b64decode("".join(INTER_B64[w])))
    return folder


def pdf_text(t: Any) -> str:
    """Inter is cut down to the characters we use; fold anything else (é → e) and drop the rest."""
    s = str(t if t is not None else "")
    s = s.replace("→", "-").replace("✓", "").replace("’", "'").replace("‘", "'")
    out = []
    for ch in s:
        if ch.isascii() or ch in "£°±·×÷–—“”•…€":
            out.append(ch)
        else:
            out.append(unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode())
    return "".join(out)


def _hex(c: str) -> Tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def build_pdf(a: Dict[str, Any], when: datetime) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=20)
    fam = "Helvetica"
    folder = _font_dir()
    if folder:
        try:
            for w, style in ((400, ""), (600, "B"), (800, "I")):
                pdf.add_font("Inter", style, os.path.join(folder, f"inter{w}.ttf"))
            fam = "Inter"
        except Exception:
            fam = "Helvetica"
    T = pdf_text if fam == "Inter" else (lambda t: pdf_text(t).encode("latin-1", "ignore").decode("latin-1"))
    heavy = "I" if fam == "Inter" else "B"
    pdf.add_page()
    W = pdf.w
    # Header band
    pdf.set_fill_color(*_hex(PURPLE))
    pdf.rect(0, 0, W, 46, "F")
    pdf.set_fill_color(*_hex(TEAL))
    pdf.rect(0, 46, W, 1.6, "F")
    pdf.set_xy(16, 13)
    pdf.set_font(fam, heavy, 17)
    pdf.set_text_color(255, 255, 255)
    first, _, rest = COMPANY.partition(" ")
    pdf.cell(pdf.get_string_width(T(first + " ")), 8, T(first + " "))
    pdf.set_text_color(*_hex(TEAL))
    pdf.cell(0, 8, T(rest))
    pdf.set_xy(16, 23)
    pdf.set_font(fam, "B", 11.5)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 6, T("Phone system quote: your answers"))
    pdf.set_xy(16, 30)
    pdf.set_font(fam, "", 9.5)
    pdf.set_text_color(201, 195, 236)
    pdf.cell(0, 5, T(f"{a.get('company', '')}  ·  Submitted {when.strftime('%d %B %Y, %H:%M')}"))
    y = 56
    for title, rows in answer_rows(a):
        need = 12 + 7 * len(rows)
        if y + need > pdf.h - 24:
            pdf.add_page()
            y = 18
        pdf.set_xy(16, y)
        pdf.set_font(fam, heavy, 10.5)
        pdf.set_text_color(*_hex(PURPLE))
        pdf.cell(0, 6, T(title.upper()))
        pdf.set_fill_color(*_hex(TEAL))
        pdf.rect(16, y + 6.6, 14, 0.8, "F")
        y += 10
        for k, v in rows:
            pdf.set_xy(16, y)
            pdf.set_font(fam, "", 9.5)
            pdf.set_text_color(107, 114, 128)
            pdf.cell(52, 6, T(k))
            pdf.set_xy(68, y)
            pdf.set_font(fam, "B", 10)
            pdf.set_text_color(31, 41, 55)
            pdf.multi_cell(W - 84, 6, T(v or "-"))
            y = max(pdf.get_y(), y + 6) + 1.2
            pdf.set_draw_color(236, 234, 245)
            pdf.line(16, y - 0.6, W - 16, y - 0.6)
        y += 5
    # Footer (on the last page; no automatic page break down here)
    pdf.set_auto_page_break(False)
    pdf.set_xy(16, pdf.h - 18)
    pdf.set_font(fam, "", 8.5)
    pdf.set_text_color(138, 143, 163)
    pdf.cell(0, 5, T(f"{COMPANY}  ·  {CONTACT['phone']}  ·  {CONTACT['email']}  ·  {CONTACT['website']}"))
    return bytes(pdf.output())


# ---------- Emails ----------
FONT_CSS = "font-family:'Segoe UI',Calibri,Arial,Helvetica,sans-serif"


def _email_shell(inner: str) -> str:
    first, _, rest = COMPANY.partition(" ")
    return (
        '<html><body style="margin:0;padding:0;background:#f1f0f7">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f1f0f7"><tr>'
        '<td align="center" style="padding:24px 12px"><table role="presentation" width="600" cellpadding="0" '
        'cellspacing="0" border="0" style="width:100%;max-width:600px"><tr><td>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" '
        'style="background:#ffffff;border-radius:12px;border:1px solid #e4e1f0">'
        f'<tr><td bgcolor="{PURPLE}" style="background:{PURPLE};padding:22px 36px;border-radius:12px 12px 0 0">'
        f'<div style="{FONT_CSS};font-size:20px;font-weight:800;color:#ffffff">{esc(first)} '
        f'<span style="color:{TEAL}">{esc(rest)}</span></div>'
        f'<div style="{FONT_CSS};font-size:12px;color:#c9c3ec;margin-top:3px">Business phones that work with your software</div>'
        f'</td></tr><tr><td height="4" bgcolor="{TEAL}" style="background:{TEAL};font-size:0;line-height:0">&nbsp;</td></tr>'
        f"{inner}</table></td></tr>"
        f'<tr><td style="padding:10px 36px 0 36px;{FONT_CSS};font-size:11px;color:#a3a7b8">'
        f'{esc(COMPANY)} · {esc(CONTACT["address"])}</td></tr></table></td></tr></table></body></html>')


def _p(text: str, extra: str = "") -> str:
    return (f'<tr><td style="padding:0 36px 14px 36px;{FONT_CSS};font-size:15px;line-height:1.6;color:#1f2937;{extra}">'
            f"{text}</td></tr>")


def _summary_table(a: Dict[str, Any]) -> str:
    rows = ""
    for title, items in answer_rows(a):
        rows += (f'<tr><td colspan="2" style="padding:14px 0 4px 0;{FONT_CSS};font-size:12px;font-weight:800;'
                 f'letter-spacing:.06em;color:{PURPLE}">{esc(title.upper())}</td></tr>')
        for k, v in items:
            rows += (f'<tr><td valign="top" style="padding:5px 12px 5px 0;border-bottom:1px solid #eeecf6;{FONT_CSS};'
                     f'font-size:13px;color:#6b7280;width:38%">{esc(k)}</td>'
                     f'<td valign="top" style="padding:5px 0;border-bottom:1px solid #eeecf6;{FONT_CSS};font-size:13px;'
                     f'font-weight:600;color:#1f2937">{esc(v).replace(chr(10), "<br>")}</td></tr>')
    return (f'<tr><td style="padding:0 36px 18px 36px"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0" style="background:#faf9fe;border:1px solid #ece9f7;border-radius:10px;'
            f'padding:4px 16px 12px 16px">{rows}</table></td></tr>')


def customer_email(a: Dict[str, Any]) -> Tuple[str, str]:
    first = (a.get("name") or "").split(" ")[0]
    steps = "".join(
        f'<tr><td valign="top" style="padding:0 12px 10px 0"><div style="width:26px;height:26px;border-radius:13px;'
        f'background:{TEAL};color:#fff;text-align:center;line-height:26px;{FONT_CSS};font-size:13px;font-weight:800">{i}</div>'
        f'</td><td style="padding:2px 0 10px 0;{FONT_CSS};font-size:14px;line-height:1.5;color:#374151">{t}</td></tr>'
        for i, t in enumerate(["We check your answers and price up the right setup.",
                               "Your no-obligation quote arrives within one working day.",
                               "A quick call to go through it, at a time that suits you."], start=1))
    sig = "<br>".join(esc(x) for x in (CONTACT["name"], CONTACT["title"]) if x)
    inner = (
        '<tr><td style="padding:28px 0 0 0;font-size:0;line-height:0">&nbsp;</td></tr>'
        + _p(f"Hi {esc(first) or 'there'},")
        + _p(f"Thanks for filling in our quick survey for <b>{esc(a.get('company'))}</b>. Here's a copy of your answers;"
             " they're also attached as a PDF for your records.")
        + _summary_table(a)
        + f'<tr><td style="padding:0 36px 6px 36px;{FONT_CSS};font-size:15px;font-weight:700;color:{PURPLE}">What happens next</td></tr>'
        + f'<tr><td style="padding:4px 36px 8px 36px"><table role="presentation" cellpadding="0" cellspacing="0" border="0">{steps}</table></td></tr>'
        + _p("Spotted something to change? Just reply to this email.", "font-size:14px;color:#4b5563")
        + f'<tr><td style="padding:6px 36px 26px 36px"><div style="{FONT_CSS};font-size:15px;color:#1f2937;margin-bottom:10px">Kind regards,</div>'
          f'<div style="border-left:3px solid {TEAL};padding:2px 0 2px 14px;{FONT_CSS}">'
          + (f'<div style="font-size:14px;color:#374151">{sig}</div>' if sig else "")
          + f'<div style="font-size:14px;font-weight:700;color:{PURPLE}">{esc(COMPANY)}</div>'
            f'<div style="font-size:13px;color:#4b5563">{esc(CONTACT["phone"])} | {esc(CONTACT["email"])}</div>'
            f'<div style="font-size:13px"><a href="https://{esc(CONTACT["website"])}" style="color:{TEAL};'
            f'text-decoration:none;font-weight:600">{esc(CONTACT["website"])}</a></div></div></td></tr>')
    return f"Your phone system answers · {COMPANY}", _email_shell(inner)


def alert_email(a: Dict[str, Any], lead_id: str, how: str, src: str) -> Tuple[str, str]:
    link = ZOHO.record_url(lead_id) if lead_id else ""
    inner = (
        '<tr><td style="padding:24px 0 0 0;font-size:0;line-height:0">&nbsp;</td></tr>'
        + _p(f"<b>{esc(a.get('company'))}</b> has just completed the quote survey"
             + (f" (from {esc(APP_NAMES.get(src, src))})" if src else "") + f". Zoho lead: {esc(how)}.")
        + (_p(f'<a href="{esc(link)}" style="color:{TEAL};font-weight:700">Open the lead in Zoho &rarr;</a>') if link else "")
        + _summary_table(a))
    return f"📋 Survey completed: {a.get('company')}", _email_shell(inner)


# =====================================================================
# Submit: Zoho + email + stats
# =====================================================================
def process_submission(a: Dict[str, Any], link: Dict[str, str]) -> Dict[str, Any]:
    when = now_uk()
    res: Dict[str, Any] = {"when": when, "errors": [], "emailed": False, "lead_id": "", "how": ""}
    res["pdf"] = build_pdf(a, when)
    lead_id = ""
    if ZOHO.configured:
        try:
            # 1. Which lead? The signed link's lead, else a lead with this email, else a new lead
            rec = ZOHO.lead(link["lid"]) if link.get("lid") else None
            how = "updated (from the email link)" if rec else ""
            if not rec:
                rec = ZOHO.find_by_email(a["email"])
                how = "updated (matched on email)" if rec else ""
            survey_vals, missing = survey_field_values(a)
            if missing:
                res["errors"].append("No Zoho field for: " + ", ".join(missing))
            if rec:
                lead_id = str(rec["id"])
                fill = {}  # Only fill blanks on the lead; never overwrite what's there
                for api, val in (("Phone", a.get("phone")), ("Street", a.get("street")), ("City", a.get("town")),
                                 ("Zip_Code", a.get("postcode")), ("Email", a.get("email"))):
                    if val and not rec.get(api):
                        fill[api] = val
                ZOHO.update_lead(lead_id, {**survey_vals, **fill})
            else:
                names = (a.get("name") or "").split()
                fields = {"Company": a.get("company"), "Last_Name": names[-1] if names else (a.get("company") or "Survey"),
                          "First_Name": " ".join(names[:-1]) if len(names) > 1 else "", "Email": a.get("email"),
                          "Phone": a.get("phone"), "Street": a.get("street"), "City": a.get("town"),
                          "Zip_Code": a.get("postcode"), **survey_vals}
                try:
                    lead_id = ZOHO.create_lead(dict(fields, Lead_Source=SURVEY_SOURCE))
                except ZohoError:  # Lead Source value not in the list: create without it
                    lead_id = ZOHO.create_lead(fields)
                how = "new lead created"
            res["lead_id"], res["how"] = lead_id, how
            # 2. Note + tag (not fatal)
            try:
                ZOHO.add_note(lead_id, "Survey completed (online form)",
                              f"Submitted {when.strftime('%d %b %Y %H:%M')} via the quote survey"
                              + (f" (link from {APP_NAMES.get(link.get('src'), link.get('src'))})" if link.get("src") else "")
                              + ".\n\n" + answers_text(a))
            except ZohoError as exc:
                res["errors"].append(f"Note: {exc}")
            try:
                ZOHO.add_tag(lead_id, SURVEY_TAG_DONE)
            except ZohoError as exc:
                res["errors"].append(f"Tag: {exc}")
            # 3. Emails: the customer's copy (with PDF), then the alert
            sender = sender_address()
            if sender:
                try:
                    att = ZOHO.upload_file(f"{re.sub(r'[^A-Za-z0-9]+', '_', a.get('company') or 'survey')}_answers.pdf",
                                           res["pdf"])
                    subj, body = customer_email(a)
                    ZOHO.send_mail(lead_id, sender, a["email"], a.get("name", ""), subj, body, [att])
                    res["emailed"] = True
                except ZohoError as exc:
                    res["errors"].append(f"Customer email: {exc}")
                if SURVEY_ALERT_TO:
                    try:
                        subj, body = alert_email(a, lead_id, how, link.get("src", ""))
                        ZOHO.send_mail(lead_id, sender, SURVEY_ALERT_TO, "", subj, body)
                    except ZohoError as exc:
                        res["errors"].append(f"Alert email: {exc}")
            else:
                res["errors"].append("No From address in Zoho")
        except ZohoError as exc:
            res["errors"].append(f"Zoho: {exc}")
    else:
        res["errors"].append("Zoho not configured")
    # 4. Stats (always, even if Zoho failed, so nothing is lost)
    key = f"{when.strftime('%Y%m%d%H%M%S')}-{lead_id or norm(a.get('email'))[:24]}"
    record = {"submitted_at": when.isoformat(timespec="seconds"), "lead_id": lead_id, "link_lead_id": link.get("lid", ""),
              "src": link.get("src", ""), "app": APP_NAMES.get(link.get("src", ""), ""), "zoho": res["how"],
              "company": a.get("company"), "contact": a.get("name"), "email": a.get("email"), "phone": a.get("phone"),
              "copy_emailed": res["emailed"], "answers": a, "problems": res["errors"]}
    err = gh_save(key, record)
    if err:
        res["errors"].append(f"Stats: {err}")
    return res


# =====================================================================
# Look & feel
# =====================================================================
CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root{{--purple:{PURPLE};--purple2:{PURPLE_2};--teal:{TEAL};--ink:#1f2937;--muted:#6b7280;--line:#e7e4f3;--bg:#f4f3fa}}
html,body,.stApp,.stApp *:not([data-testid="stIconMaterial"]):not(.material-symbols-rounded),button,input,textarea,select,label{{font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif!important}}
:root,.stApp{{color-scheme:light}}
.stApp{{background:var(--bg)!important;color:var(--ink)!important}}
/* Same light look in dark mode: text, inputs, pills, alerts and code all get explicit colours */
.stApp .stMarkdown p,.stApp .stMarkdown li,.stApp .stMarkdown strong,.stApp [data-testid="stCaptionContainer"],
.stApp [data-testid="stText"]{{color:var(--ink)}}
.stApp [data-testid="stMarkdownContainer"] code{{color:var(--purple);background:#efedf8}}
[data-testid="stTextInputRootElement"],[data-testid="stTextAreaRootElement"],[data-testid="stNumberInputContainer"],
[data-baseweb="input"],[data-baseweb="textarea"]{{background:#faf9fe!important;border-color:#e2def0!important}}
.stApp input,.stApp textarea{{background:transparent!important;color:var(--ink)!important;-webkit-text-fill-color:var(--ink)!important;
  caret-color:var(--purple)}}
.stApp input::placeholder,.stApp textarea::placeholder{{color:#9aa0b4!important;-webkit-text-fill-color:#9aa0b4!important}}
[data-testid="stTextInputRootElement"]:focus-within,[data-testid="stTextAreaRootElement"]:focus-within,
[data-testid="stNumberInputContainer"]:focus-within{{border-color:var(--teal)!important;box-shadow:0 0 0 3px rgba(0,181,163,.18)!important}}
input:-webkit-autofill,input:-webkit-autofill:hover,input:-webkit-autofill:focus{{-webkit-text-fill-color:var(--ink)!important;
  -webkit-box-shadow:0 0 0 1000px #eefaf8 inset!important}}
[data-testid="stNumberInput"] button{{background:#f1effa!important;color:var(--purple)!important;border-color:#e2def0!important}}
[data-testid="stNumberInput"] button:hover{{background:var(--teal)!important;color:#fff!important}}
[data-testid="stNumberInput"] button svg{{fill:currentColor}}
[data-testid="stButtonGroup"] button,[data-testid="stPills"] button{{background:#fff!important;color:#374151!important;
  border:1px solid #dcd8ec!important;transition:all .15s}}
[data-testid="stButtonGroup"] button p,[data-testid="stPills"] button p{{color:inherit!important}}
[data-testid="stButtonGroup"] button:hover,[data-testid="stPills"] button:hover{{background:#eefaf8!important;
  border-color:var(--teal)!important;color:var(--purple)!important}}
[data-testid="stButtonGroup"] button[kind*="Active"],[data-testid="stPills"] button[kind*="Active"],
[data-testid*="pillsActive"],[data-testid="stButtonGroup"] button[aria-checked="true"],
[data-testid="stButtonGroup"] button[aria-pressed="true"]{{background:var(--teal)!important;border-color:var(--teal)!important;
  color:#fff!important;font-weight:600!important}}
[data-testid="stButtonGroup"] button[kind*="Active"]:hover,[data-testid*="pillsActive"]:hover{{filter:brightness(1.05)}}
[data-testid="stAlert"]{{background:#fff4f2!important;border:1px solid #f6c9c0!important;border-radius:12px!important}}
[data-testid="stAlert"] *{{color:#7a2617!important}}
.stApp button[kind="secondary"]:not([data-testid*="pills"]){{background:#fff!important;color:var(--purple)!important;
  border:1px solid #dcd8ec!important}}
.stApp [data-testid="stCode"] pre,.stApp pre{{background:#f6f5fb!important}}
.stApp [data-testid="stCode"] code,.stApp pre code{{color:var(--purple)!important}}
header[data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stDecoration"],#MainMenu,footer,
[data-testid="stStatusWidget"],[data-testid="collapsedControl"],[data-testid="stSidebarCollapsedControl"]{{display:none!important}}
.block-container{{max-width:760px!important;padding:0 1rem 3rem 1rem!important}}
/* Hero */
.qs-hero{{margin:0 -1rem 22px -1rem;background:linear-gradient(135deg,var(--purple) 0%,var(--purple2) 100%);
  padding:34px 32px 30px 32px;border-bottom:4px solid var(--teal);border-radius:0 0 22px 22px;position:relative;overflow:hidden}}
.qs-hero:after{{content:"";position:absolute;right:-80px;top:-80px;width:260px;height:260px;border-radius:50%;
  background:radial-gradient(circle,rgba(0,181,163,.35),transparent 65%)}}
.qs-brand{{font-weight:800;font-size:1.25rem;color:#fff;letter-spacing:.2px}}.qs-brand span{{color:var(--teal)}}
.qs-tag{{font-size:.78rem;color:#c9c3ec;margin-top:2px}}
.qs-title{{font-size:2rem;font-weight:800;color:#fff;line-height:1.15;margin-top:26px;letter-spacing:-.02em}}
.qs-title span{{color:var(--teal)}}
.qs-sub{{font-size:.98rem;color:#dcd8f3;margin-top:10px;line-height:1.55;max-width:560px}}
.qs-meta{{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}}
.qs-meta div{{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18);color:#fff;font-size:.78rem;
  font-weight:600;padding:6px 12px;border-radius:999px}}
/* Progress */
.qs-prog{{display:flex;align-items:center;gap:12px;margin:2px 2px 16px 2px}}
.qs-bar{{flex:1;height:8px;border-radius:99px;background:#e4e1f0;overflow:hidden}}
.qs-bar i{{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--teal),#3fd3c2);transition:width .4s}}
.qs-prog b{{font-size:.8rem;color:var(--purple);white-space:nowrap}}
/* Cards */
div[class*="st-key-card-"]{{background:#fff;border:1px solid var(--line);border-radius:18px;padding:22px 24px 10px 24px;
  margin-bottom:16px;box-shadow:0 1px 2px rgba(31,20,80,.04),0 8px 24px -12px rgba(31,20,80,.12)}}
.qs-h{{display:flex;align-items:center;gap:12px;margin-bottom:6px}}
.qs-num{{width:32px;height:32px;border-radius:10px;background:var(--purple);color:#fff;font-weight:800;font-size:.9rem;
  display:flex;align-items:center;justify-content:center;flex:none}}
.qs-num.ok{{background:var(--teal)}}
.qs-h .t{{font-weight:800;color:var(--purple);font-size:1.08rem}}.qs-h .s{{font-size:.82rem;color:var(--muted)}}
/* Inputs */
.stTextInput input,.stNumberInput input,.stTextArea textarea{{border-radius:10px!important}}
[data-testid="stTextInputRootElement"],[data-testid="stTextAreaRootElement"],[data-testid="stNumberInputContainer"]{{border-radius:10px!important}}
label p{{font-weight:600!important;color:#374151!important;font-size:.88rem!important}}
[data-testid="stPills"] button,[data-testid="stButtonGroup"] button{{border-radius:999px!important}}
/* Submit */
.st-key-submit button,.st-key-dl button{{background:linear-gradient(135deg,var(--teal),#00a090)!important;border:0!important;
  color:#fff!important;font-weight:800!important;font-size:1.02rem!important;padding:.8rem 1rem!important;border-radius:12px!important;
  box-shadow:0 10px 24px -10px rgba(0,181,163,.7)}}
.st-key-submit button p,.st-key-dl button p{{font-weight:800!important;font-size:1.02rem!important;color:#fff!important}}
.st-key-submit button:hover,.st-key-dl button:hover{{filter:brightness(1.05)}}
[data-testid="stElementContainer"]:has(iframe[height="0"]),.element-container:has(iframe[height="0"]){{display:none!important}}
.st-key-hp{{position:absolute!important;left:-9999px!important;height:0!important;overflow:hidden!important}}
.qs-fine{{font-size:.76rem;color:var(--muted);text-align:center;margin-top:10px;line-height:1.5}}
.qs-foot{{text-align:center;font-size:.76rem;color:#9aa0b4;margin-top:26px;line-height:1.6}}
.qs-foot a{{color:var(--teal);text-decoration:none;font-weight:600}}
/* Thanks */
.qs-done{{background:#fff;border:1px solid var(--line);border-radius:18px;padding:24px;margin-bottom:16px;
  box-shadow:0 8px 24px -12px rgba(31,20,80,.12)}}
.qs-tick{{width:54px;height:54px;border-radius:16px;background:rgba(0,181,163,.12);color:var(--teal);display:flex;
  align-items:center;justify-content:center;margin-bottom:12px}}
.qs-done h3{{margin:0 0 6px 0;color:var(--purple);font-weight:800;font-size:1.25rem}}
.qs-done p{{margin:0;color:#4b5563;font-size:.95rem;line-height:1.55}}
.qs-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-bottom:16px}}
@media (max-width:640px){{.qs-grid{{grid-template-columns:1fr}}.qs-title{{font-size:1.6rem}}.qs-hero{{padding:26px 22px}}}}
.qs-sum{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px 18px}}
.qs-sum .h{{font-size:.72rem;font-weight:800;letter-spacing:.08em;color:var(--teal);text-transform:uppercase;margin-bottom:8px}}
.qs-sum .r{{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-top:1px solid #f0eef7;font-size:.86rem}}
.qs-sum .r:first-of-type{{border-top:0}}
.qs-sum .k{{color:var(--muted)}}.qs-sum .v{{color:var(--ink);font-weight:600;text-align:right;white-space:pre-line}}
.qs-steps{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px 20px;margin-bottom:16px}}
.qs-steps .t{{font-weight:800;color:var(--purple);margin-bottom:10px}}
.qs-step{{display:flex;gap:12px;align-items:flex-start;margin-bottom:10px;font-size:.92rem;color:#374151}}
.qs-step i{{font-style:normal;width:26px;height:26px;border-radius:9px;background:var(--purple);color:#fff;font-weight:800;
  font-size:.8rem;display:flex;align-items:center;justify-content:center;flex:none}}
.qs-cta{{display:block;text-align:center;background:var(--purple);color:#fff!important;font-weight:700;padding:13px;
  border-radius:12px;text-decoration:none!important;margin-top:4px}}
</style>
"""
TICK_SVG = ('<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>')


def _full_width() -> Dict[str, Any]:
    import inspect
    try:
        if "width" in inspect.signature(st.button).parameters:
            return {"width": "stretch"}
    except (TypeError, ValueError):
        pass
    return {"use_container_width": True}


FULL = _full_width()


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def brand_html() -> str:
    first, _, rest = COMPANY.partition(" ")
    return (f'<div class="qs-brand">{esc(first)} <span>{esc(rest)}</span></div>'
            '<div class="qs-tag">Business phones that work with your software</div>')


def footer() -> None:
    html(f'<div class="qs-foot">{esc(COMPANY)} · {esc(CONTACT["address"])}<br>'
         f'<a href="tel:{esc(CONTACT["phone"].replace(" ", ""))}">{esc(CONTACT["phone"])}</a> · '
         f'<a href="mailto:{esc(CONTACT["email"])}">{esc(CONTACT["email"])}</a> · '
         f'<a href="https://{esc(CONTACT["website"])}" target="_blank">{esc(CONTACT["website"])}</a><br>'
         "We only use your answers to prepare your quote.</div>")


def section(key: str, num: int, title: str, sub: str, done: bool):
    box = st.container(key=f"card-{key}")
    with box:
        html(f'<div class="qs-h"><div class="qs-num{" ok" if done else ""}">{"✓" if done else num}</div>'
             f'<div><div class="t">{esc(title)}</div><div class="s">{esc(sub)}</div></div></div>')
    return box


def pills(label: str, options: List[str], key: str, multi: bool):
    try:
        return st.pills(label, options, selection_mode="multi" if multi else "single", key=key)
    except AttributeError:  # Older Streamlit
        if multi:
            return st.multiselect(label, options, key=key)
        return st.radio(label, options, key=key, index=None, horizontal=True)


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,24}$")
POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.I)


# =====================================================================
# Pages
# =====================================================================
def read_link() -> Dict[str, str]:
    """The details carried in the email link. Only trusted (tied to a lead) when the signature checks out."""
    qp = st.query_params
    link = {k: str(qp.get(k) or "").strip()[:200] for k in ("lid", "c", "n", "e", "p", "src", "s")}
    ok = bool(SIGNING_KEY and link["lid"] and link["s"]
              and hmac.compare_digest(link["s"], sign(link["lid"], link["e"])))
    if not ok:
        link["lid"] = ""
    link["src"] = link["src"] if link["src"] in APP_NAMES else ""
    return link


AUTOFILL_JS = """
<script>
(function () {
  const doc = window.parent.document, win = window.parent;
  if (win.__qsAutofill) return;
  win.__qsAutofill = true;
  const setVal = (el) => {
    const proto = el.tagName === "TEXTAREA" ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, el.value);
    el.dispatchEvent(new win.Event("input", { bubbles: true }));
    el.dispatchEvent(new win.FocusEvent("focusout", { bubbles: true }));
    el.dispatchEvent(new win.FocusEvent("blur"));
  };
  const sync = (all) => {
    doc.querySelectorAll('.stApp input[type="text"], .stApp input:not([type]), .stApp textarea').forEach((el) => {
      if (el.value !== (el.__qsSeen ?? el.defaultValue)) {
        if (all || el !== doc.activeElement) setVal(el);
      }
      el.__qsSeen = el.value;
    });
  };
  // Autofill changes boxes you're not typing in: pick those up every half second
  win.setInterval(() => sync(false), 500);
  // And make sure everything is in before Send is pressed
  doc.addEventListener("pointerdown", (e) => {
    if (e.target.closest && e.target.closest(".st-key-submit")) sync(true);
  }, true);
})();
</script>
"""


def form_page() -> None:
    components.html(AUTOFILL_JS, height=0)
    link = read_link()
    if "qs_started" not in st.session_state:  # Prefill once from the link
        st.session_state["qs_started"] = time.time()
        for k, f in (("company", "c"), ("name", "n"), ("email", "e"), ("phone", "p")):
            if link.get(f):
                st.session_state[f"f_{k}"] = link[f]
    first = (st.session_state.get("f_name") or link.get("n") or "").split(" ")[0]
    html('<div class="qs-hero">' + brand_html()
         + f'<div class="qs-title">{("Hi " + esc(first) + ", let") if first else "Let"}\'s get your <span>quote</span> right</div>'
         + '<div class="qs-sub">Five quick sections about your sites, team and phone numbers. It takes about two minutes,'
           + (" and we've filled in what we already know." if link.get("c") else "")
         + ' Your answers let us prepare an accurate, no-obligation quote without a long call.</div>'
         '<div class="qs-meta"><div>⏱ About 2 minutes</div><div>🔒 No obligation</div><div>📄 Copy emailed to you</div></div></div>')

    ss = st.session_state
    g = lambda k: ss.get(f"f_{k}")  # noqa: E731
    done1 = all(str(g(k) or "").strip() for k in ("company", "name", "email", "phone"))
    done2 = all(str(g(k) or "").strip() for k in ("street", "town", "postcode"))
    done3 = bool(g("users")) and bool(g("call_pref"))
    done4 = bool(g("numbers"))
    done5 = bool(g("system") or g("system_age"))
    n_done = sum([done1, done2, done3, done4, done5])
    html(f'<div class="qs-prog"><div class="qs-bar"><i style="width:{n_done * 20}%"></i></div>'
         f'<b>{n_done} of 5 complete</b></div>')

    with section("1", 1, "About you", "So we know who the quote is for", done1):
        c1, c2 = st.columns(2)  # Two rows, so phones show them in reading order
        c1.text_input("Company name", key="f_company", autocomplete="organization", placeholder="e.g. Acme Dental Ltd")
        c2.text_input("Your name", key="f_name", autocomplete="name", placeholder="First and last name")
        c3, c4 = st.columns(2)
        c3.text_input("Email address", key="f_email", autocomplete="email", placeholder="you@company.co.uk")
        c4.text_input("Phone number", key="f_phone", autocomplete="tel", placeholder="Best number to reach you")

    with section("2", 2, "Your sites", "Where the phones will be used", done2):
        st.text_input("Main business address", key="f_street", autocomplete="street-address", placeholder="Building and street")
        c1, c2 = st.columns(2)
        c1.text_input("Town or city", key="f_town", autocomplete="address-level2")
        c2.text_input("Postcode", key="f_postcode", autocomplete="postal-code")
        st.number_input("How many sites do you have?", min_value=1, max_value=200, step=1, key="f_sites", value=1)
        if (g("sites") or 1) > 1:
            st.text_area("Other site addresses", key="f_other_sites", height=90,
                         placeholder="One per line, with postcode")

    with section("3", 3, "Your team", "Who makes and takes calls, and how", done3):
        st.number_input("How many people need to make or take calls?", min_value=1, max_value=5000, step=1,
                        key="f_users", value=None, placeholder="e.g. 8")
        pills("How would your team like to take calls? Pick any that apply", CALL_OPTIONS, "f_call_pref", multi=True)
        if "Desk phones" in (g("call_pref") or []):
            st.number_input("How many desk handsets do you need?", min_value=1, max_value=5000, step=1,
                            key="f_handsets", value=None, placeholder="e.g. 6")

    with section("4", 4, "Phone numbers", "Keeping your numbers is easy and free of downtime", done4):
        pills("What would you like to do with your numbers?", NUMBER_OPTIONS, "f_numbers", multi=False)
        if g("numbers") in (NUMBER_OPTIONS[0], NUMBER_OPTIONS[2]):
            st.text_area("Which number(s) would you like to keep?", key="f_port_numbers", height=80,
                         placeholder="Main number first, e.g. 01743 123456")

    with section("5", 5, "Your current phone system", "Optional, but it helps us plan the switch", done5):
        st.text_input("Make and model", key="f_system", placeholder="e.g. BT Versatility, Panasonic KX-NS500")
        pills("Roughly how old is it?", AGE_OPTIONS, "f_system_age", multi=False)
        st.text_area("Anything else we should know?", key="f_notes", height=90,
                     placeholder="Call queues, opening hours, software you use, contract end dates…")

    with st.container(key="hp"):  # Hidden from people; bots fill it in
        st.text_input("Leave this empty", key="f_website", autocomplete="off")

    if ss.get("qs_errors"):
        st.error("Nearly there. Please check:\n\n" + "\n".join(f"- {e}" for e in ss["qs_errors"]))
    if st.button("Send my answers  →", key="submit", type="primary", **FULL):
        submit(link)
    html('<div class="qs-fine">You\'ll get a copy of your answers by email straight away.</div>')
    footer()


def collect() -> Tuple[Dict[str, Any], List[str]]:
    ss = st.session_state
    t = lambda k: str(ss.get(f"f_{k}") or "").strip()  # noqa: E731
    a = {k: t(k) for k in ("company", "name", "email", "phone", "street", "town", "postcode", "other_sites",
                           "port_numbers", "system", "notes")}
    a["postcode"] = a["postcode"].upper()
    a["sites"] = int(ss.get("f_sites") or 1)
    a["users"] = int(ss["f_users"]) if ss.get("f_users") else None
    a["call_pref"] = list(ss.get("f_call_pref") or [])
    a["handsets"] = int(ss["f_handsets"]) if ss.get("f_handsets") and "Desk phones" in a["call_pref"] else None
    a["numbers"] = ss.get("f_numbers") or ""
    a["system_age"] = ss.get("f_system_age") or ""
    if a["sites"] <= 1:
        a["other_sites"] = ""
    if a["numbers"] == NUMBER_OPTIONS[1]:
        a["port_numbers"] = ""
    errs = []
    for k, label in (("company", "Company name"), ("name", "Your name"), ("email", "Email address"),
                     ("phone", "Phone number"), ("street", "Main business address"), ("town", "Town or city"),
                     ("postcode", "Postcode")):
        if not a[k]:
            errs.append(f"{label} is missing")
    if a["email"] and not EMAIL_RE.match(a["email"]):
        errs.append("The email address doesn't look right")
    if a["postcode"] and not POSTCODE_RE.match(a["postcode"]):
        errs.append("The postcode doesn't look like a UK postcode")
    if not a["users"]:
        errs.append("How many people need to make or take calls")
    if not a["call_pref"]:
        errs.append("How your team would like to take calls")
    if not a["numbers"]:
        errs.append("What you'd like to do with your phone numbers")
    return a, errs


def submit(link: Dict[str, str]) -> None:
    ss = st.session_state
    a, errs = collect()
    ss["qs_errors"] = errs
    if errs:
        st.rerun()
    if ss.get("f_website") or time.time() - ss.get("qs_started", 0) < 5:  # Bot: pretend all went well
        ss["qs_result"] = {"answers": a, "bot": True, "when": now_uk(), "emailed": False, "pdf": b"", "errors": []}
        st.rerun()
    with st.spinner("Sending your answers securely…"):
        res = process_submission(a, link)
    res["answers"] = a
    ss["qs_result"] = res
    st.rerun()


def thanks_page() -> None:
    res = st.session_state["qs_result"]
    a = res["answers"]
    first = (a.get("name") or "").split(" ")[0]
    html('<div class="qs-hero">' + brand_html()
         + f'<div class="qs-title">Thank you{", " + esc(first) if first else ""}. <span>That\'s everything.</span></div>'
         + '<div class="qs-sub">Your answers are with our team. Here\'s a summary for your records.</div></div>')
    copy_line = (f"A copy is on its way to <b>{esc(a.get('email'))}</b>, with a PDF of your answers attached."
                 if res.get("emailed") else "Download a copy of your answers below for your records.")
    html(f'<div class="qs-done"><div class="qs-tick">{TICK_SVG}</div><h3>We\'ve got your answers</h3>'
         f'<p>{copy_line} We\'ll be in touch within one working day with your no-obligation quote.</p></div>')
    cards = ""
    for title, rows in answer_rows(a):
        cards += (f'<div class="qs-sum"><div class="h">{esc(title)}</div>'
                  + "".join(f'<div class="r"><span class="k">{esc(k)}</span><span class="v">{esc(v)}</span></div>' for k, v in rows)
                  + "</div>")
    html(f'<div class="qs-grid">{cards}</div>')
    if res.get("pdf"):
        st.download_button("Download my answers (PDF)  ↓", res["pdf"], key="dl", type="primary", **FULL,
                           file_name=f"{re.sub(r'[^A-Za-z0-9]+', '_', a.get('company') or 'Your')}_phone_system_answers.pdf",
                           mime="application/pdf")
    steps = "".join(f'<div class="qs-step"><i>{i}</i><div>{t}</div></div>' for i, t in enumerate([
        "We check your answers and price up the right setup for your team.",
        "Your no-obligation quote arrives by email within one working day.",
        "A quick call to walk through it, at a time that suits you."], start=1))
    cta = (f'<a class="qs-cta" href="{esc(BOOKING_URL)}" target="_blank">Book a time to talk it through →</a>'
           if BOOKING_URL else "")
    html(f'<div class="qs-steps" style="margin-top:16px"><div class="t">What happens next</div>{steps}{cta}</div>')
    footer()


def admin_page() -> None:
    html('<div class="qs-hero">' + brand_html() + '<div class="qs-title">Survey <span>admin</span></div>'
         '<div class="qs-sub">Status checks, the Zoho key helper and test links. Customers never see this page.</div></div>')
    pw = secret("ADMIN_PASSWORD")
    if not pw:
        st.error("Add ADMIN_PASSWORD to this app's Secrets to use the admin page.")
        return
    if not st.session_state.get("qs_admin"):
        typed = st.text_input("Admin password", type="password")
        if typed and hmac.compare_digest(typed.encode(), pw.encode()):
            st.session_state["qs_admin"] = True
            st.rerun()
        elif typed:
            st.error("Wrong password.")
        return
    with st.container(key="card-a1"):
        html('<div class="qs-h"><div class="qs-num">1</div><div><div class="t">Status</div>'
             '<div class="s">Everything the form needs</div></div></div>')
        checks = []
        if ZOHO.configured:
            try:
                ZOHO._token(force=True)
                checks.append(("Zoho connection", True, "Signed in"))
                fields = lead_fields()
                _, missing = survey_field_values({"users": 1, "handsets": 1, "call_pref": ["Desk phones"], "sites": 2,
                                                  "other_sites": "x", "numbers": NUMBER_OPTIONS[0], "port_numbers": "x",
                                                  "system": "x", "system_age": AGE_OPTIONS[0]})
                checks.append(("Survey fields on Leads", not missing,
                               "All found" if not missing else "Not found: " + ", ".join(missing)))
                src_f = fields.get("Lead_Source") or {}
                has_src = any(norm(o.get("display_value")) == norm(SURVEY_SOURCE) for o in src_f.get("pick_list_values") or [])
                checks.append((f"Lead Source '{SURVEY_SOURCE}'", has_src, "In the list" if has_src else "Add it to Lead Source"))
                snd = sender_address()
                ok_from = bool(snd) and (snd.get("email") or "").lower() == SURVEY_FROM
                checks.append(("Send copies from", ok_from, (snd or {}).get("email", "none") if ok_from
                               else f"{SURVEY_FROM} isn't a verified From address in Zoho; using {(snd or {}).get('email', 'none')}"))
            except ZohoError as exc:
                checks.append(("Zoho connection", False, str(exc)))
        else:
            checks.append(("Zoho connection", False, "Add ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN"))
        try:
            data, _ = gh_read()
            checks.append(("Stats repo", bool(gh_settings()[0] and gh_settings()[1]), f"{len(data)} responses saved"))
        except Exception as exc:
            checks.append(("Stats repo", False, f"Can't read it ({exc.__class__.__name__})"))
        checks.append(("Signed links", bool(SIGNING_KEY), "Set" if SIGNING_KEY else "Add SURVEY_SIGNING_KEY (same in all apps)"))
        checks.append(("Alerts to", bool(SURVEY_ALERT_TO), SURVEY_ALERT_TO or "Off (add SURVEY_ALERT_TO)"))
        for name, ok, detail in checks:
            st.markdown(f"{'✅' if ok else '⚠️'} **{name}**: {detail}")
    with st.container(key="card-a2"):
        html('<div class="qs-h"><div class="qs-num">2</div><div><div class="t">Zoho key helper</div>'
             '<div class="s">Swap a Self Client code for the refresh token</div></div></div>')
        st.markdown("In **api-console.zoho.eu** open your Self Client → **Generate Code**, paste this scope, choose"
                    " 10 minutes, then paste the code below straight away.")
        st.code(ZOHO_SCOPE, language=None)
        code = st.text_input("Code from Zoho", type="password", key="adm_code")
        if st.button("Get my refresh token", key="adm_swap") and code:
            st.session_state["adm_swap_res"] = ZOHO.exchange_code(code)
        r = st.session_state.get("adm_swap_res") or {}
        if r.get("error"):
            st.error(r["error"])
        elif r.get("refresh_token"):
            st.success("Done. Put this line in this app's Secrets, save, then reboot:")
            st.code(f'ZOHO_REFRESH_TOKEN = "{r["refresh_token"]}"', language=None)
    with st.container(key="card-a3"):
        html('<div class="qs-h"><div class="qs-num">3</div><div><div class="t">Test link</div>'
             '<div class="s">The same signed link the apps put in their emails</div></div></div>')
        c1, c2 = st.columns(2)
        lid = c1.text_input("Zoho lead ID", key="adm_lid")
        comp = c2.text_input("Company", key="adm_c")
        name = c1.text_input("Contact name", key="adm_n")
        mail = c2.text_input("Email", key="adm_e")
        base = secret("APP_URL", "https://your-survey-app.streamlit.app")
        if lid or comp:
            st.code(signed_link(base, lid, comp, name, mail, src="pe"), language=None)
    footer()


# =====================================================================
html(CSS)
if st.query_params.get("admin"):
    admin_page()
elif st.session_state.get("qs_result"):
    thanks_page()
else:
    form_page()
