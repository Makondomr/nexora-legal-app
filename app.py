from datetime import date, datetime, timedelta
from io import BytesIO
import os
import uuid
import hashlib
import json
import re
import hmac
import secrets
import time
import pandas as pd
import streamlit as st
import database as db

from twilio.rest import Client as TwilioClient

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    KeepTogether,
)

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="Nexora Legal",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded"
)

db.init_db()

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ============================================================
# STYLE
# ============================================================

st.markdown("""
<style>
:root {
    --bg: #f4f7f6;
    --panel: #ffffff;
    --primary: #0f5f4b;
    --primary-dark: #0b4638;
    --accent: #d7f3e7;
    --muted: #6b7280;
    --border: #e5e7eb;
}

.stApp {
    background: var(--bg);
}

section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0f5f4b 0%, #0b4638 100%);
}

section[data-testid="stSidebar"] * {
    color: #ffffff !important;
}

.block-container {
    padding-top: 1.5rem;
    padding-bottom: 2rem;
}

.nx-hero {
    background: linear-gradient(135deg, #0f5f4b 0%, #0b4638 100%);
    border-radius: 18px;
    padding: 26px 30px;
    color: #fff;
    margin-bottom: 1rem;
    box-shadow: 0 8px 20px rgba(15, 95, 75, .16);
}

.nx-hero h1 {
    margin: 0;
    font-size: 30px;
}

.nx-hero p {
    margin: 6px 0 0;
    color: #d7f3e7;
}

.nx-card {
    background: white;
    border: 1px solid #e5e7eb;
    border-radius: 16px;
    padding: 18px;
    margin-bottom: 12px;
    box-shadow: 0 4px 14px rgba(0, 0, 0, .04);
}

.nx-soft {
    background: #eef8f4;
    border: 1px solid #cfe9df;
    border-radius: 14px;
    padding: 14px 16px;
    margin-bottom: 14px;
}

div[data-testid="stMetric"] {
    background: white;
    border: 1px solid #e5e7eb;
    border-radius: 14px;
    padding: 14px 16px;
    box-shadow: 0 4px 14px rgba(0,0,0,.03);
}

.stButton > button,
.stFormSubmitButton > button {
    border-radius: 10px;
    font-weight: 600;
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# SESSION
# ============================================================

def clear_session():
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()


def login_user(user):
    st.session_state.authenticated = True
    st.session_state.user_id = user["id"]
    st.session_state.org_id = user["org_id"]
    st.session_state.firm_number = user["firm_number"]
    st.session_state.firm_name = user["firm_name"]
    st.session_state.user_name = user["name"]
    st.session_state.cell = user["cell"]
    st.session_state.role = user["role"]
    st.session_state.attorney_level = (
        user.get("practitioner_type")
        or user.get("attorney_level")
        or ""
    )
    st.session_state.practitioner_type_id = user.get("practitioner_type_id")
    st.session_state.page = "Dashboard"


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False


# ============================================================
# HELPERS
# ============================================================

def money(value):
    try:
        return f"R {float(value or 0):,.2f}"
    except Exception:
        return "R 0.00"


def due_text(due_date):
    if not due_date:
        return "No due date"

    try:
        d = datetime.strptime(
            str(due_date)[:10],
            "%Y-%m-%d"
        ).date()

        diff = (d - date.today()).days

        if diff < 0:
            return f"Overdue by {-diff} day(s)"
        if diff == 0:
            return "Due today"
        if diff <= 2:
            return f"Due in {diff} day(s)"
        return f"{diff} day(s) remaining"

    except Exception:
        return str(due_date)


def task_days_remaining(due_date):
    """Return whole days remaining to a task due date, or None if invalid."""
    if not due_date:
        return None
    try:
        d = datetime.strptime(str(due_date)[:10], "%Y-%m-%d").date()
        return (d - date.today()).days
    except Exception:
        return None


def task_attention_label(due_date):
    days = task_days_remaining(due_date)
    if days is None:
        return None
    if days < 0:
        return f"🚨 Overdue by {-days} day(s)"
    if days == 0:
        return "🔴 Due today"
    if days == 1:
        return "🔴 Due tomorrow"
    if days == 2:
        return "⚠️ Due in 2 days"
    return None


def save_upload(uploaded, prefix):
    if not uploaded:
        return None

    filename = (
        f"{prefix}_{uuid.uuid4().hex}_"
        f"{os.path.basename(uploaded.name)}"
    )

    path = os.path.join(UPLOAD_DIR, filename)

    with open(path, "wb") as f:
        f.write(uploaded.getbuffer())

    return path


def admin_access():
    return st.session_state.role == "Admin"


def send_whatsapp_message(cell, body=None, otp_code=None):
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    content_sid = os.getenv("TWILIO_WHATSAPP_CONTENT_SID", "").strip()

    if not all([account_sid, auth_token, from_number, content_sid]):
        raise RuntimeError(
            "Twilio WhatsApp OTP is not fully configured. "
            "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_WHATSAPP_FROM and TWILIO_WHATSAPP_CONTENT_SID."
        )

    # Existing callers pass the OTP inside the old body text.
    # Extract the 6-digit code so the rest of the app does not need to change.
    code = str(otp_code or "").strip()
    if not code:
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(body or ""))
        if match:
            code = match.group(1)

    if not re.fullmatch(r"\d{6}", code):
        raise RuntimeError("Could not determine the 6-digit OTP for the WhatsApp template.")

    client = TwilioClient(account_sid, auth_token)

    message = client.messages.create(
        from_=from_number,
        to=normalize_whatsapp_number(cell),
        content_sid=content_sid,
        content_variables=json.dumps({"1": code}),
    )

    return message




def send_firm_approval_whatsapp(firm):
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    approval_content_sid = os.getenv("TWILIO_WHATSAPP_APPROVAL_CONTENT_SID", "").strip()

    if not all([account_sid, auth_token, from_number, approval_content_sid]):
        raise RuntimeError(
            "Firm approval WhatsApp is not fully configured. "
            "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM "
            "and TWILIO_WHATSAPP_APPROVAL_CONTENT_SID."
        )

    if not firm:
        raise RuntimeError("Approved firm details were not returned by the database.")

    cell = firm.get("registered_cell") or firm.get("cell")
    firm_number = str(firm.get("firm_number") or "").strip()

    if not cell:
        raise RuntimeError("The approved firm has no registered cellphone number.")
    if not firm_number:
        raise RuntimeError("The approved firm has no Firm Number.")

    client = TwilioClient(account_sid, auth_token)
    return client.messages.create(
        from_=from_number,
        to=normalize_whatsapp_number(cell),
        content_sid=approval_content_sid,
        content_variables=json.dumps({"1": firm_number}),
    )


def login_super_admin(user):
    st.session_state.authenticated = True
    st.session_state.is_super_admin = True
    st.session_state.super_admin_id = user["id"]
    st.session_state.super_admin_name = user["name"]
    st.session_state.super_admin_cell = user["cell"]
    st.session_state.page = "Platform Dashboard"
    db.mark_super_admin_login(user["id"])

def clear_super_admin_otp_state():
    for key in [
        "super_otp_user",
        "super_otp_hash",
        "super_otp_expires_at",
        "super_otp_attempts",
        "super_otp_last_sent_at",
    ]:
        st.session_state.pop(key, None)

def send_super_admin_otp(user):
    code = f"{secrets.randbelow(1_000_000):06d}"

    send_whatsapp_message(
        user["cell"],
        (
            f"Nexora Super Admin verification code: {code}. "
            "This code expires in 5 minutes. "
            "Do not share this code with anyone."
        ),
    )

    st.session_state.super_otp_user = dict(user)
    st.session_state.super_otp_hash = _otp_digest(
        code,
        "NEXORA-SUPER-ADMIN",
        str(user["cell"])
    )
    st.session_state.super_otp_expires_at = time.time() + 300
    st.session_state.super_otp_attempts = 0
    st.session_state.super_otp_last_sent_at = time.time()

def verify_super_admin_otp(code):
    user = st.session_state.get("super_otp_user")
    expected_hash = st.session_state.get("super_otp_hash")
    expires_at = st.session_state.get("super_otp_expires_at", 0)
    attempts = int(st.session_state.get("super_otp_attempts", 0))

    if not user or not expected_hash:
        return False, "No active Super Admin verification code."

    if time.time() > float(expires_at):
        clear_super_admin_otp_state()
        return False, "The verification code has expired. Request a new OTP."

    if attempts >= 5:
        clear_super_admin_otp_state()
        return False, "Too many incorrect attempts. Request a new OTP."

    entered = str(code or "").strip()
    if not (entered.isdigit() and len(entered) == 6):
        return False, "Enter the 6-digit verification code."

    candidate = _otp_digest(
        entered,
        "NEXORA-SUPER-ADMIN",
        str(user["cell"])
    )

    if not hmac.compare_digest(candidate, expected_hash):
        st.session_state.super_otp_attempts = attempts + 1
        remaining = 5 - st.session_state.super_otp_attempts
        if remaining <= 0:
            clear_super_admin_otp_state()
            return False, "Too many incorrect attempts. Request a new OTP."
        return False, f"Incorrect verification code. {remaining} attempt(s) remaining."

    verified = user
    clear_super_admin_otp_state()
    login_super_admin(verified)
    return True, "Super Admin verified."

def normalize_whatsapp_number(cell):
    """Convert common South African cellphone formats to WhatsApp E.164."""
    raw = "".join(ch for ch in str(cell or "").strip() if ch.isdigit() or ch == "+")
    if raw.startswith("0"):
        raw = "+27" + raw[1:]
    elif raw.startswith("27") and not raw.startswith("+"):
        raw = "+" + raw
    elif not raw.startswith("+"):
        raw = "+" + raw
    return f"whatsapp:{raw}"


def _otp_digest(code, firm_number, cell):
    secret = os.getenv("TWILIO_AUTH_TOKEN", "")
    payload = f"{firm_number}|{cell}|{code}|{secret}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def clear_otp_state():
    for key in ["otp_pending_user", "otp_hash", "otp_expires_at",
                "otp_attempts", "otp_last_sent_at"]:
        st.session_state.pop(key, None)


def send_login_otp(user):
    code = f"{secrets.randbelow(1_000_000):06d}"
    firm_number = str(user["firm_number"])
    cell = str(user["cell"])

    # Use the approved WhatsApp Authentication template, exactly like Super Admin OTP.
    message = send_whatsapp_message(cell, otp_code=code)

    st.session_state.otp_pending_user = dict(user)
    st.session_state.otp_hash = _otp_digest(code, firm_number, cell)
    st.session_state.otp_expires_at = time.time() + 300
    st.session_state.otp_attempts = 0
    st.session_state.otp_last_sent_at = time.time()
    return message.sid

def verify_login_otp(code):
    user = st.session_state.get("otp_pending_user")
    expected_hash = st.session_state.get("otp_hash")
    expires_at = st.session_state.get("otp_expires_at", 0)
    attempts = int(st.session_state.get("otp_attempts", 0))

    if not user or not expected_hash:
        return False, "No active verification code. Request a new OTP."

    if time.time() > float(expires_at):
        clear_otp_state()
        return False, "The verification code has expired. Request a new OTP."

    if attempts >= 5:
        clear_otp_state()
        return False, "Too many incorrect attempts. Request a new OTP."

    entered = str(code or "").strip()
    if not (entered.isdigit() and len(entered) == 6):
        return False, "Enter the 6-digit verification code."

    candidate = _otp_digest(
        entered, str(user["firm_number"]), str(user["cell"])
    )

    if not hmac.compare_digest(candidate, expected_hash):
        st.session_state.otp_attempts = attempts + 1
        remaining = 5 - st.session_state.otp_attempts
        if remaining <= 0:
            clear_otp_state()
            return False, "Too many incorrect attempts. Request a new OTP."
        return False, f"Incorrect verification code. {remaining} attempt(s) remaining."

    verified_user = user
    clear_otp_state()
    login_user(verified_user)
    return True, "Verification successful."


def generate_invoice_pdf(org_id, invoice_id):
    """
    Generate an invoice PDF in memory using data already captured
    in Nexora: firm details, client details, matter/task billing,
    totals, payment terms and banking details.
    """
    invoice = db.get_invoice(org_id, invoice_id)

    if not invoice:
        raise ValueError("Invoice could not be found.")

    firm = db.get_organization(org_id)
    client = db.get_client(org_id, invoice["client_id"])
    items = db.list_invoice_items(org_id, invoice_id)
    paid = db.get_invoice_amount_paid(org_id, invoice_id)

    if not firm:
        raise ValueError("Firm details could not be found.")

    if not client:
        raise ValueError("Client details could not be found.")

    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=16 * mm,
        title=invoice["invoice_number"],
        author=firm.get("name") or "Nexora Legal",
    )

    styles = getSampleStyleSheet()

    firm_title = ParagraphStyle(
        "FirmTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=17,
        leading=20,
        alignment=TA_CENTER,
        spaceAfter=4,
    )

    centered = ParagraphStyle(
        "Centered",
        parent=styles["BodyText"],
        fontSize=8.5,
        leading=11,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#475569"),
    )

    section = ParagraphStyle(
        "Section",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#0F5F4B"),
        spaceBefore=7,
        spaceAfter=5,
    )

    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontSize=8.8,
        leading=11.5,
        textColor=colors.HexColor("#1F2937"),
    )

    body_right = ParagraphStyle(
        "BodyRight",
        parent=body,
        alignment=TA_RIGHT,
    )

    small = ParagraphStyle(
        "Small",
        parent=styles["BodyText"],
        fontSize=7.6,
        leading=9.5,
        textColor=colors.HexColor("#475569"),
    )

    story = []

    # ---------------- Firm-generated letterhead ----------------
    story.append(
        Paragraph(
            str(firm.get("name") or "").upper(),
            firm_title
        )
    )

    header_bits = []

    if firm.get("registration_number"):
        header_bits.append(
            f"Registration: {firm['registration_number']}"
        )

    if firm.get("vat_number"):
        header_bits.append(
            f"VAT: {firm['vat_number']}"
        )

    if firm.get("address"):
        header_bits.append(
            str(firm["address"]).replace("\n", "<br/>")
        )

    contact_bits = []

    if firm.get("phone"):
        contact_bits.append(str(firm["phone"]))

    if firm.get("email"):
        contact_bits.append(str(firm["email"]))

    if firm.get("website"):
        contact_bits.append(str(firm["website"]))

    if contact_bits:
        header_bits.append(" | ".join(contact_bits))

    if header_bits:
        story.append(
            Paragraph(
                "<br/>".join(header_bits),
                centered
            )
        )

    story.append(Spacer(1, 5 * mm))

    # ---------------- Invoice summary ----------------
    invoice_summary = Table(
        [
            [
                Paragraph(
                    "<b>INVOICE</b>",
                    ParagraphStyle(
                        "InvoiceHeading",
                        parent=styles["Heading1"],
                        fontSize=18,
                        textColor=colors.HexColor("#0F5F4B"),
                    )
                ),
                Paragraph(
                    f"<b>{invoice['invoice_number']}</b>",
                    ParagraphStyle(
                        "InvoiceNumber",
                        parent=body_right,
                        fontSize=11,
                    )
                )
            ],
            [
                Paragraph(
                    f"Invoice Date: {invoice.get('invoice_date') or '-'}<br/>"
                    f"Due Date: {invoice.get('due_date') or '-'}",
                    body
                ),
                Paragraph(
                    f"Status: <b>{invoice.get('status') or '-'}</b>",
                    body_right
                )
            ]
        ],
        colWidths=[105 * mm, 68 * mm],
    )

    invoice_summary.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 1), (-1, 1), 0.5, colors.HexColor("#CBD5E1")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])
    )

    story.append(invoice_summary)
    story.append(Spacer(1, 5 * mm))

    # ---------------- Client details ----------------
    story.append(Paragraph("BILL TO", section))

    client_lines = [
        f"<b>{client.get('name') or ''}</b>",
        f"Client Number: {client.get('client_number') or '-'}",
    ]

    if client.get("address"):
        client_lines.append(
            str(client["address"]).replace("\n", "<br/>")
        )

    if client.get("email"):
        client_lines.append(
            f"Email: {client['email']}"
        )

    if client.get("phone"):
        client_lines.append(
            f"Phone: {client['phone']}"
        )

    story.append(
        Paragraph(
            "<br/>".join(client_lines),
            body
        )
    )

    story.append(Spacer(1, 4 * mm))

    # ---------------- Invoice line items ----------------
    story.append(
        Paragraph(
            "LEGAL SERVICES / BILLING ITEMS",
            section
        )
    )

    table_data = [
        [
            Paragraph("<b>Matter</b>", small),
            Paragraph("<b>Description</b>", small),
            Paragraph("<b>Practitioner</b>", small),
            Paragraph("<b>Qty</b>", small),
            Paragraph("<b>Rate</b>", small),
            Paragraph("<b>Fees</b>", small),
            Paragraph("<b>Disbursement</b>", small),
        ]
    ]

    for item in items:
        table_data.append([
            Paragraph(
                str(item.get("matter_number") or "-"),
                small
            ),
            Paragraph(
                str(item.get("description") or "-"),
                small
            ),
            Paragraph(
                str(item.get("practitioner_type") or "-"),
                small
            ),
            Paragraph(
                f"{float(item.get('quantity') or 0):,.2f}",
                small
            ),
            Paragraph(
                money(item.get("rate")),
                small
            ),
            Paragraph(
                money(item.get("amount")),
                small
            ),
            Paragraph(
                money(item.get("disbursement_amount")),
                small
            ),
        ])

    item_table = Table(
        table_data,
        colWidths=[
            25 * mm,
            42 * mm,
            27 * mm,
            12 * mm,
            20 * mm,
            23 * mm,
            26 * mm,
        ],
        repeatRows=1,
    )

    item_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F5F0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0F5F4B")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ])
    )

    story.append(item_table)
    story.append(Spacer(1, 5 * mm))

    # ---------------- Totals ----------------
    fees_subtotal = float(invoice.get("fees_subtotal") or 0)
    disbursement_total = float(invoice.get("disbursement_total") or 0)
    subtotal = float(invoice.get("subtotal") or (fees_subtotal + disbursement_total))
    vat_amount = float(invoice.get("vat_amount") or 0)
    total_amount = float(invoice.get("total_amount") or 0)
    outstanding = max(total_amount - float(paid or 0), 0)

    totals = Table(
        [
            ["Professional Fees", money(fees_subtotal)],
            ["Disbursements", money(disbursement_total)],
            ["Subtotal", money(subtotal)],
            [
                f"VAT ({float(invoice.get('vat_rate') or 0):g}%)",
                money(vat_amount)
            ],
            ["TOTAL", money(total_amount)],
            ["Paid", money(paid)],
            ["Outstanding", money(outstanding)],
        ],
        colWidths=[45 * mm, 35 * mm],
        hAlign="RIGHT"
    )

    totals.setStyle(
        TableStyle([
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("FONTNAME", (0, 4), (-1, 4), "Helvetica-Bold"),
            ("FONTNAME", (0, 6), (-1, 6), "Helvetica-Bold"),
            ("LINEABOVE", (0, 4), (-1, 4), 0.7, colors.HexColor("#64748B")),
            ("LINEABOVE", (0, 6), (-1, 6), 0.5, colors.HexColor("#CBD5E1")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
    )

    story.append(totals)
    story.append(Spacer(1, 6 * mm))

    # ---------------- Banking ----------------
    banking = []

    if firm.get("bank_name"):
        banking.append(
            f"Bank: {firm['bank_name']}"
        )

    if firm.get("bank_account_name"):
        banking.append(
            f"Account Name: {firm['bank_account_name']}"
        )

    if firm.get("bank_account_number"):
        banking.append(
            f"Account Number: {firm['bank_account_number']}"
        )

    if firm.get("bank_branch_code"):
        banking.append(
            f"Branch Code: {firm['bank_branch_code']}"
        )

    if banking:
        story.append(
            KeepTogether([
                Paragraph(
                    "BANKING DETAILS",
                    section
                ),
                Paragraph(
                    "<br/>".join(banking),
                    body
                ),
            ])
        )

    payment_terms = (
        invoice.get("payment_terms")
        or firm.get("invoice_payment_terms")
    )

    if payment_terms:
        story.append(Spacer(1, 3 * mm))
        story.append(
            Paragraph(
                f"<b>Payment Terms:</b> {payment_terms}",
                body
            )
        )

    if invoice.get("notes"):
        story.append(Spacer(1, 3 * mm))
        story.append(
            Paragraph(
                f"<b>Invoice Notes:</b> {invoice['notes']}",
                body
            )
        )

    story.append(Spacer(1, 7 * mm))
    story.append(
        Paragraph(
            "Generated by Nexora Legal.",
            centered
        )
    )

    doc.build(story)

    buffer.seek(0)
    return buffer.getvalue()


# ============================================================
# LOGIN / REGISTER
# ============================================================

if not st.session_state.authenticated:

    st.markdown("""
    <div class="nx-hero">
        <h1>⚖️ NEXORA LEGAL</h1>
        <p>Legal Practice Management Platform</p>
    </div>
    """, unsafe_allow_html=True)

    tab_login, tab_register, tab_super = st.tabs(
        ["🔐 Login", "🏢 Register New Firm", "🛡️ Super Admin"]
    )

    with tab_login:

        st.subheader("Login")
        otp_user = st.session_state.get("otp_pending_user")

        if not otp_user:
            st.caption(
                "Enter your Nexora Firm Number and registered cellphone number. "
                "A 6-digit verification code will be sent to your WhatsApp."
            )

            with st.form("login_form"):
                firm_number = st.text_input("Firm Number", placeholder="NEX-001")
                cell = st.text_input("Cellphone Number", placeholder="0831234567")

                if st.form_submit_button("Send WhatsApp OTP", use_container_width=True):
                    user = db.authenticate_user(firm_number, cell)

                    if not user:
                        st.error("Login failed. Check the Firm Number and Cellphone Number.")
                    elif str(user.get("organization_status") or "").upper() != "ACTIVE":
                        status = str(user.get("organization_status") or "PENDING").upper()
                        if status == "PENDING":
                            st.warning("This firm is still pending Nexora approval.")
                        elif status == "SUSPENDED":
                            st.error("This firm's Nexora access is suspended.")
                        elif status == "REJECTED":
                            st.error("This firm registration was not approved.")
                        else:
                            st.error(f"This firm cannot log in while its status is {status}.")
                    else:
                        try:
                            send_login_otp(user)
                            st.success("Verification code sent to your registered WhatsApp number.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not send WhatsApp verification code: {e}")

        else:
            st.success("A 6-digit verification code was sent to your registered WhatsApp number.")
            st.caption(f"Firm: {otp_user['firm_number']} • User: {otp_user['name']}")

            with st.form("otp_verify_form"):
                otp_code = st.text_input(
                    "Verification Code", max_chars=6, placeholder="123456"
                )

                if st.form_submit_button("Verify & Login", use_container_width=True):
                    ok, message = verify_login_otp(otp_code)
                    if ok:
                        st.success(message)
                        st.rerun()
                    else:
                        st.error(message)

            c1, c2 = st.columns(2)

            with c1:
                elapsed = time.time() - float(
                    st.session_state.get("otp_last_sent_at", 0)
                )

                if elapsed >= 60:
                    if st.button("Resend OTP", use_container_width=True):
                        try:
                            send_login_otp(otp_user)
                            st.success("A new verification code was sent.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not resend WhatsApp verification code: {e}")
                else:
                    remaining = max(1, 60 - int(elapsed))
                    st.caption(f"You can request a new code in {remaining} second(s).")

            with c2:
                if st.button("Use Different Login", use_container_width=True):
                    clear_otp_state()
                    st.rerun()

    with tab_register:

        st.subheader("Register a Law Firm")

        st.info(
            "The person registering the firm automatically becomes the Firm Administrator and initial Director."
        )

        with st.form("firm_registration_form"):

            firm_name = st.text_input("Firm Name")
            admin_name = st.text_input("Administrator Full Name")
            admin_cell = st.text_input("Administrator Cellphone Number")

            if st.form_submit_button(
                "Register Firm",
                use_container_width=True
            ):

                success, message, data = db.register_new_firm(
                    firm_name,
                    admin_name,
                    admin_cell
                )

                if success:
                    st.success("Firm registered successfully.")
                    st.write(f"**Firm Name:** {data['firm_name']}")
                    st.write(f"**Firm Number:** `{data['firm_number']}`")
                    st.write(f"**Administrator:** {data['name']}")
                    st.write(f"**Cellphone:** `{data['cell']}`")
                    st.write("**Role:** Admin")
                    st.write("**Practitioner Type:** Director")

                    st.info(
                        "Registration submitted and is pending Nexora approval. "
                        "Once approved, use the Firm Number and registered cellphone number to log in."
                    )
                else:
                    st.error(message)

    with tab_super:

        st.subheader("Nexora Super Admin")
        st.caption("Super Admin access is protected by WhatsApp OTP.")

        configured_cell = os.getenv("NEXORA_SUPER_ADMIN_CELL", "0837938103").strip()

        # Ensure the pilot Super Admin exists in the database.
        super_user = db.get_super_admin_by_cell(configured_cell)
        if not super_user:
            db.ensure_super_admin(
                os.getenv("NEXORA_SUPER_ADMIN_NAME", "Mzamani Russell Makondo").strip(),
                configured_cell
            )
            super_user = db.get_super_admin_by_cell(configured_cell)

        if not super_user:
            st.error("Could not initialise the Nexora Super Admin account.")
        else:
            super_otp_user = st.session_state.get("super_otp_user")

            if not super_otp_user:
                with st.form("super_admin_login_form"):
                    super_cell = st.text_input(
                        "Super Admin Cellphone Number",
                        placeholder="0831234567"
                    )

                    if st.form_submit_button(
                        "Send Super Admin OTP",
                        use_container_width=True
                    ):
                        entered_cell = db.normalize_cell(super_cell)
                        expected_cell = db.normalize_cell(configured_cell)

                        if entered_cell != expected_cell:
                            st.error("Super Admin access denied.")
                        else:
                            try:
                                send_super_admin_otp(super_user)
                                st.success("Super Admin verification code sent to WhatsApp.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Could not send Super Admin verification code: {e}")
            else:
                st.success("A 6-digit Super Admin verification code was sent to your WhatsApp.")

                with st.form("super_admin_otp_form"):
                    super_code = st.text_input(
                        "Verification Code",
                        max_chars=6,
                        placeholder="123456"
                    )

                    if st.form_submit_button(
                        "Verify & Open Control Centre",
                        use_container_width=True
                    ):
                        ok, message = verify_super_admin_otp(super_code)
                        if ok:
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)

                c1, c2 = st.columns(2)
                with c1:
                    elapsed = time.time() - float(
                        st.session_state.get("super_otp_last_sent_at", 0)
                    )
                    if elapsed >= 60:
                        if st.button("Resend Super Admin OTP", use_container_width=True):
                            try:
                                send_super_admin_otp(super_user)
                                st.success("A new Super Admin verification code was sent.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Could not resend Super Admin verification code: {e}")
                    else:
                        remaining = max(1, 60 - int(elapsed))
                        st.caption(f"You can request a new code in {remaining} second(s).")

                with c2:
                    if st.button("Cancel Super Admin Login", use_container_width=True):
                        clear_super_admin_otp_state()
                        st.rerun()


    st.stop()

# NEXORA SUPER ADMIN CONTROL CENTRE
# ============================================================

if st.session_state.get("is_super_admin"):

    super_admin_id = st.session_state.super_admin_id
    super_admin_name = st.session_state.super_admin_name

    with st.sidebar:
        st.markdown("## 🛡️ NEXORA")
        st.write("**Super Admin Control Centre**")
        st.caption(f"User: {super_admin_name}")

        st.divider()

        super_pages = [
            "Platform Dashboard",
            "Pending Registrations",
            "Firm Management",
            "Subscriptions & Billing",
            "Support Queries",
            "Audit Trail",
        ]

        if st.session_state.get("page") not in super_pages:
            st.session_state.page = "Platform Dashboard"

        super_menu = st.radio(
            "Platform Navigation",
            super_pages,
            index=super_pages.index(st.session_state.page),
            label_visibility="collapsed"
        )
        st.session_state.page = super_menu

        st.divider()

        if st.button("🚪 Super Admin Logout", use_container_width=True):
            clear_session()

    st.markdown(
        """
        <div class="nx-hero">
            <h1>Nexora Super Admin</h1>
            <p>Platform Control Centre</p>
        </div>
        """,
        unsafe_allow_html=True
    )

    if super_menu == "Platform Dashboard":

        st.header("Platform Dashboard")
        metrics = db.platform_dashboard_metrics()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Active Firms", metrics["active_firms"])
        c2.metric("Pending Approval", metrics["pending_firms"])
        c3.metric("Active Users", metrics["active_users"])
        c4.metric("Open Support Queries", metrics["open_support"])

        c5, c6, c7 = st.columns(3)
        c5.metric("Suspended Firms", metrics["suspended_firms"])
        c6.metric("Monthly Recurring Revenue", money(metrics["mrr"]))
        c7.metric("Total Firms", metrics["total_firms"])

        st.subheader("Recent Firm Registrations")
        firms = db.list_platform_firms()
        if firms:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Firm Number": f["firm_number"],
                        "Firm": f["name"],
                        "Cellphone": f["registered_cell"],
                        "Status": f["status"],
                        "Users": f["active_users"],
                        "Subscription": f.get("subscription_status") or "Not Set",
                        "Monthly Fee": money(f.get("monthly_fee")),
                        "Registered": f["created_at"],
                    }
                    for f in firms[:15]
                ]),
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("No firms registered yet.")

    elif super_menu == "Pending Registrations":

        st.header("Pending Firm Registrations")
        pending_firms = db.list_platform_firms("PENDING")

        if not pending_firms:
            st.success("There are no pending firm registrations.")
        else:
            for firm in pending_firms:
                with st.container(border=True):
                    st.subheader(
                        f"{firm['name']} — {firm['firm_number']}"
                    )
                    c1, c2, c3 = st.columns(3)
                    c1.write(f"**Registered Cell:** {firm['registered_cell']}")
                    c2.write(f"**Users:** {firm['active_users']}")
                    c3.write(f"**Registered:** {firm['created_at']}")

                    users = db.list_platform_firm_users(firm["id"])
                    if users:
                        st.dataframe(
                            pd.DataFrame([
                                {
                                    "Name": u["name"],
                                    "Cellphone": u["cell"],
                                    "Role": u["role"],
                                    "Practitioner Type": u.get("practitioner_type") or "",
                                }
                                for u in users
                            ]),
                            use_container_width=True,
                            hide_index=True
                        )

                    approve_col, reject_col = st.columns(2)

                    with approve_col:
                        if st.button(
                            "✅ Approve Firm",
                            key=f"approve_firm_{firm['id']}",
                            use_container_width=True
                        ):
                            try:
                                approved = db.approve_firm(
                                    firm["id"],
                                    super_admin_id
                                )
                                try:
                                    send_firm_approval_whatsapp(approved)
                                    st.success(
                                        f"{approved['firm_number']} approved and "
                                        "Firm Number sent by WhatsApp."
                                    )
                                except Exception as wa_error:
                                    st.warning(
                                        f"Firm approved, but WhatsApp could not be sent: {wa_error}"
                                    )
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

                    with reject_col:
                        with st.form(f"reject_form_{firm['id']}"):
                            reject_reason = st.text_input(
                                "Reason for rejection",
                                key=f"reject_reason_{firm['id']}"
                            )
                            if st.form_submit_button(
                                "❌ Reject Registration",
                                use_container_width=True
                            ):
                                try:
                                    db.reject_firm(
                                        firm["id"],
                                        super_admin_id,
                                        reject_reason
                                    )
                                    st.success("Registration rejected.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(str(e))

    elif super_menu == "Firm Management":

        st.header("Firm Management")
        firms = db.list_platform_firms()

        if not firms:
            st.info("No firms registered.")
        else:
            firm_map = {
                f"{f['firm_number']} — {f['name']} [{f['status']}]": f
                for f in firms
            }
            selected = firm_map[
                st.selectbox("Select Firm", list(firm_map.keys()))
            ]

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Firm Number", selected["firm_number"])
            c2.metric("Status", selected["status"])
            c3.metric("Active Users", selected["active_users"])
            c4.metric(
                "Monthly Fee",
                money(selected.get("monthly_fee"))
            )

            st.write(f"**Registered Cell:** {selected['registered_cell']}")
            st.write(f"**Registered:** {selected['created_at']}")

            users = db.list_platform_firm_users(selected["id"])
            if users:
                st.subheader("Firm Users")
                st.dataframe(
                    pd.DataFrame([
                        {
                            "Name": u["name"],
                            "Cellphone": u["cell"],
                            "Role": u["role"],
                            "Practitioner Type": u.get("practitioner_type") or "",
                            "Active": "Yes" if u["active"] else "No",
                        }
                        for u in users
                    ]),
                    use_container_width=True,
                    hide_index=True
                )

            if selected["status"] == "ACTIVE":
                with st.form("suspend_selected_firm"):
                    reason = st.text_input("Suspension Reason")
                    if st.form_submit_button(
                        "Suspend Firm",
                        use_container_width=True
                    ):
                        db.suspend_firm(
                            selected["id"],
                            super_admin_id,
                            reason
                        )
                        st.success("Firm suspended.")
                        st.rerun()

            elif selected["status"] == "SUSPENDED":
                if st.button(
                    "Reactivate Firm",
                    use_container_width=True
                ):
                    db.reactivate_firm(
                        selected["id"],
                        super_admin_id
                    )
                    st.success("Firm reactivated.")
                    st.rerun()

            elif selected["status"] == "PENDING":
                st.info(
                    "This firm is pending approval. Use Pending Registrations."
                )

            elif selected["status"] == "REJECTED":
                if st.button(
                    "Approve Previously Rejected Firm",
                    use_container_width=True
                ):
                    approved = db.approve_firm(
                        selected["id"],
                        super_admin_id
                    )
                    try:
                        send_firm_approval_whatsapp(approved)
                    except Exception as wa_error:
                        st.warning(
                            f"Approved, but WhatsApp failed: {wa_error}"
                        )
                    st.success("Firm approved.")
                    st.rerun()

    elif super_menu == "Subscriptions & Billing":

        st.header("Nexora Subscriptions & Billing")
        firms = db.list_platform_firms()

        if not firms:
            st.info("No firms available.")
        else:
            firm_map = {
                f"{f['firm_number']} — {f['name']}": f
                for f in firms
            }
            selected = firm_map[
                st.selectbox(
                    "Firm",
                    list(firm_map.keys()),
                    key="subscription_firm"
                )
            ]

            details = db.get_platform_firm(selected["id"]) or selected

            with st.form("subscription_form"):
                package_name = st.selectbox(
                    "Package",
                    ["Trial", "Standard", "Group", "Custom"],
                    index=(
                        ["Trial", "Standard", "Group", "Custom"].index(
                            details.get("package_name")
                        )
                        if details.get("package_name")
                        in ["Trial", "Standard", "Group", "Custom"]
                        else 1
                    )
                )

                monthly_fee = st.number_input(
                    "Monthly Fee (R)",
                    min_value=0.0,
                    value=float(details.get("monthly_fee") or 0),
                    step=50.0
                )

                subscription_status = st.selectbox(
                    "Subscription Status",
                    ["Trial", "Active", "Overdue", "Paused", "Cancelled"],
                    index=(
                        ["Trial", "Active", "Overdue", "Paused", "Cancelled"].index(
                            details.get("subscription_status")
                        )
                        if details.get("subscription_status")
                        in ["Trial", "Active", "Overdue", "Paused", "Cancelled"]
                        else 0
                    )
                )

                start_date_value = st.date_input(
                    "Start Date",
                    value=date.today()
                )

                next_billing_value = st.date_input(
                    "Next Billing Date",
                    value=date.today() + timedelta(days=30)
                )

                subscription_notes = st.text_area(
                    "Subscription / Billing Notes",
                    value=details.get("subscription_notes") or ""
                )

                if st.form_submit_button(
                    "Save Subscription",
                    use_container_width=True
                ):
                    db.upsert_platform_subscription(
                        selected["id"],
                        super_admin_id,
                        package_name,
                        monthly_fee,
                        subscription_status,
                        start_date_value.isoformat(),
                        next_billing_value.isoformat(),
                        subscription_notes
                    )
                    st.success("Subscription updated.")
                    st.rerun()

            st.subheader("Subscription Portfolio")
            portfolio = db.list_platform_firms()
            st.dataframe(
                pd.DataFrame([
                    {
                        "Firm": f["name"],
                        "Firm Number": f["firm_number"],
                        "Firm Status": f["status"],
                        "Package": f.get("package_name") or "Not Set",
                        "Subscription": f.get("subscription_status") or "Not Set",
                        "Monthly Fee": float(f.get("monthly_fee") or 0),
                        "Next Billing": f.get("next_billing_date") or "",
                    }
                    for f in portfolio
                ]),
                use_container_width=True,
                hide_index=True
            )

    elif super_menu == "Support Queries":

        st.header("Support Queries")
        queries = db.list_support_queries()

        if not queries:
            st.info("No support queries.")
        else:
            for q in queries:
                with st.expander(
                    f"#{q['id']} • {q['firm_number']} • {q['subject']} • {q['status']}"
                ):
                    st.write(f"**Firm:** {q['firm_name']}")
                    st.write(f"**Raised by:** {q.get('user_name') or 'Unknown'}")
                    st.write(f"**Priority:** {q['priority']}")
                    st.write(q["description"])

                    with st.form(f"support_update_{q['id']}"):
                        status = st.selectbox(
                            "Status",
                            ["Open", "In Progress", "Resolved"],
                            index=(
                                ["Open", "In Progress", "Resolved"].index(q["status"])
                                if q["status"] in ["Open", "In Progress", "Resolved"]
                                else 0
                            ),
                            key=f"status_{q['id']}"
                        )
                        notes = st.text_area(
                            "Super Admin Notes",
                            value=q.get("admin_notes") or "",
                            key=f"notes_{q['id']}"
                        )

                        if st.form_submit_button("Update Query"):
                            db.update_support_query(
                                q["id"],
                                super_admin_id,
                                status,
                                notes
                            )
                            st.success("Support query updated.")
                            st.rerun()

    elif super_menu == "Audit Trail":

        st.header("Platform Audit Trail")
        audit = db.list_platform_audit()

        if not audit:
            st.info("No platform audit activity yet.")
        else:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Date": a["created_at"],
                        "Action": a["action"],
                        "Firm Number": a.get("firm_number") or "",
                        "Firm": a.get("firm_name") or "",
                        "Super Admin": a.get("super_admin_name") or "",
                        "Details": a.get("details") or "",
                    }
                    for a in audit
                ]),
                use_container_width=True,
                hide_index=True
            )

    st.stop()


# ============================================================
# CONTEXT
# ============================================================

user_id = st.session_state.user_id
org_id = st.session_state.org_id
firm_number = st.session_state.firm_number
firm_name = st.session_state.firm_name
user_name = st.session_state.user_name
user_role = st.session_state.role
user_cell = st.session_state.cell

current_user = db.get_user(user_id)

if not current_user:
    clear_session()

attorney_level = (
    current_user.get("practitioner_type")
    or current_user.get("attorney_level")
    or ""
)

practitioner_type_id = current_user.get("practitioner_type_id")

st.session_state.attorney_level = attorney_level
st.session_state.practitioner_type_id = practitioner_type_id


# ============================================================



# ============================================================
# CONTEXT
# ============================================================

user_id = st.session_state.user_id
org_id = st.session_state.org_id
firm_number = st.session_state.firm_number
firm_name = st.session_state.firm_name
user_name = st.session_state.user_name
user_role = st.session_state.role
user_cell = st.session_state.cell

current_user = db.get_user(user_id)

if not current_user:
    clear_session()

attorney_level = (
    current_user.get("practitioner_type")
    or current_user.get("attorney_level")
    or ""
)

practitioner_type_id = current_user.get("practitioner_type_id")

st.session_state.attorney_level = attorney_level
st.session_state.practitioner_type_id = practitioner_type_id


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.markdown("## ⚖️ NEXORA")
    st.write(f"**{firm_name}**")
    st.caption(f"Firm: {firm_number}")
    st.caption(f"User: {user_name}")
    st.caption(f"Type: {attorney_level}")
    st.caption(f"Role: {user_role}")

    st.divider()

    pages = [
        "Dashboard",
        "Clients",
        "Matters & Tasks",
        "Documents",
        "Communications",
        "Billing & Invoices",
        "Actuarial & Damages",
        "AI Assistant",
    ]

    if admin_access():
        pages.append("Admin & Firm Settings")

    if "page" not in st.session_state:
        st.session_state.page = "Dashboard"

    if st.session_state.page not in pages:
        st.session_state.page = "Dashboard"

    menu = st.radio(
        "Navigation",
        pages,
        index=pages.index(st.session_state.page),
        label_visibility="collapsed"
    )

    st.session_state.page = menu

    st.divider()

    if st.button(
        "🚪 Logout",
        use_container_width=True
    ):
        clear_session()


# ============================================================
# HEADER
# ============================================================

st.markdown(
    f"""
    <div class="nx-hero">
        <h1>{firm_name}</h1>
        <p>{firm_number} • {user_name} • {attorney_level}</p>
    </div>
    """,
    unsafe_allow_html=True
)


# ============================================================
# DASHBOARD
# ============================================================

if menu == "Dashboard":

    st.header("Practice Dashboard")

    st.markdown("""
    <div class="nx-soft">
        This tab gives a quick view of clients, matters, your open tasks and billing.
    </div>
    """, unsafe_allow_html=True)

    clients = db.list_clients(org_id)
    matters = db.list_matters(org_id)
    tasks = db.list_tasks(org_id)
    billing = db.get_billing_summary(org_id)

    my_open_tasks = [
        t for t in tasks
        if int(t["user_id"]) == int(user_id)
        and t["status"] != "Complete"
    ]

    attention_tasks = [
        t for t in tasks
        if t["status"] != "Complete"
        and task_attention_label(t.get("due_date")) is not None
    ]

    c1, c2, c3, c4, c5 = st.columns(5)

    c1.metric("Registered Clients", len(clients))
    c2.metric(
        "Open Matters",
        len([m for m in matters if m["status"] != "Closed"])
    )
    c3.metric("My Open Tasks", len(my_open_tasks))
    c4.metric("Tasks Requiring Attention", len(attention_tasks))
    c5.metric("Outstanding", money(billing["total_outstanding"]))

    if attention_tasks:
        st.warning(
            f"The firm has {len(attention_tasks)} open task(s) due within 2 days or already overdue."
        )
        st.dataframe(
            pd.DataFrame([
                {
                    "Task": x["task_number"],
                    "Client": x["client_name"],
                    "Matter": x["matter_number"],
                    "Service": x["service_name"],
                    "Practitioner": x.get("practitioner_name") or "-",
                    "Due Date": x["due_date"],
                    "Warning": task_attention_label(x.get("due_date")),
                }
                for x in sorted(
                    attention_tasks,
                    key=lambda row: task_days_remaining(row.get("due_date")) or 0
                )
            ]),
            width="stretch",
            hide_index=True
        )

    st.divider()
    st.subheader("My Active Tasks")

    if not my_open_tasks:
        st.success("You have no open tasks.")
    else:
        for task in my_open_tasks:
            with st.container(border=True):
                st.write(f"**{task['task_number']}**")
                st.write(f"Client: **{task['client_name']}**")
                st.write(f"Matter: `{task['matter_number']}`")
                st.write(f"Task Type / Service: **{task['service_name']}**")
                st.write(f"Task: {task['title']}")
                attention = task_attention_label(task.get("due_date"))
                if attention:
                    st.error(attention)
                else:
                    st.caption(due_text(task["due_date"]))


# ============================================================
# CLIENTS
# ============================================================

elif menu == "Clients":

    st.header("Clients")

    st.markdown("""
    <div class="nx-soft">
        Register and manage clients here. General clients use the firm's General Fee Schedule.
        SLA clients get their own client-specific rate card by practitioner type during registration.
    </div>
    """, unsafe_allow_html=True)

    tab_register, tab_directory = st.tabs(
        ["➕ Register Client", "Client Directory"]
    )

    with tab_register:

        practitioner_types = db.list_practitioner_types(org_id)

        with st.form("client_registration_form"):

            c1, c2 = st.columns(2)

            with c1:
                client_name = st.text_input("Client Full Name / Entity Name")
                client_type = st.selectbox(
                    "Client Type",
                    ["Individual", "Company", "Trust", "Estate", "Other"]
                )
                email = st.text_input("Email Address")
                phone = st.text_input("Cellphone / Telephone")

            with c2:
                address = st.text_area("Physical / Postal Address")
                reference = st.text_input(
                    "Client Reference / ID / Registration Number"
                )
                billing_type = st.selectbox(
                    "Billing Type",
                    ["General", "SLA"]
                )
                notes = st.text_area("Client Notes")

            sla_rates = {}

            if billing_type == "SLA":

                st.markdown("### SLA Rate Card")

                st.caption(
                    "These are the negotiated rates for this client. "
                    "They depend only on practitioner type, not on the task/service."
                )

                if not practitioner_types:
                    st.error(
                        "No Practitioner Types exist yet. "
                        "Go to Admin & Firm Settings → Practitioner Types first."
                    )
                else:
                    cols = st.columns(2)

                    for idx, pt in enumerate(practitioner_types):
                        with cols[idx % 2]:
                            sla_rates[pt["id"]] = st.number_input(
                                f"{pt['name']} Rate (R per hour)",
                                min_value=0.0,
                                value=0.0,
                                step=100.0,
                                key=f"sla_rate_{pt['id']}"
                            )

            if st.form_submit_button(
                "Register Client",
                use_container_width=True
            ):

                if not client_name.strip():
                    st.error("Client name is required.")

                elif billing_type == "SLA" and not practitioner_types:
                    st.error(
                        "Create Practitioner Types before registering an SLA client."
                    )

                elif (
                    billing_type == "SLA"
                    and any(rate <= 0 for rate in sla_rates.values())
                ):
                    st.error(
                        "Enter an SLA rate greater than zero for every practitioner type."
                    )

                else:
                    try:
                        client_id, client_number = db.create_client(
                            org_id,
                            client_name.strip(),
                            client_type,
                            email.strip(),
                            phone.strip(),
                            address.strip(),
                            reference.strip(),
                            notes.strip(),
                            billing_type,
                            sla_rates if billing_type == "SLA" else None
                        )

                        st.success(
                            f"Client {client_name.strip()} successfully registered to {firm_name}."
                        )
                        st.write(f"**Client Number:** `{client_number}`")

                    except Exception as e:
                        st.error(f"Could not register client: {e}")

    with tab_directory:

        clients = db.list_clients(org_id)

        if not clients:
            st.info("No clients registered yet.")
        else:
            for client in clients:
                with st.container(border=True):
                    st.subheader(client["name"])
                    st.write(f"**Client Number:** `{client['client_number']}`")
                    st.write(f"**Billing Type:** {client['billing_type']}")
                    st.write(f"**Email:** {client.get('email') or '-'}")
                    st.write(f"**Phone:** {client.get('phone') or '-'}")
                    st.write(f"**Address:** {client.get('address') or '-'}")

                    if client["billing_type"] == "SLA":
                        rates = db.list_client_sla_rates(
                            org_id,
                            client["id"]
                        )

                        if rates:
                            st.markdown("**SLA Rate Card**")
                            st.dataframe(
                                pd.DataFrame([
                                    {
                                        "Practitioner Type": r["practitioner_type"],
                                        "Rate": r["rate"],
                                        "Unit": r["unit"]
                                    }
                                    for r in rates
                                ]),
                                use_container_width=True,
                                hide_index=True
                            )

                    with st.expander("Upload Client Document"):

                        with st.form(f"client_doc_{client['id']}"):

                            doc_name = st.text_input("Document Name / Type")
                            uploaded = st.file_uploader(
                                "Choose File",
                                type=["pdf", "docx", "png", "jpg", "jpeg"]
                            )

                            if st.form_submit_button("Upload Document"):

                                if not doc_name.strip():
                                    st.error("Document name is required.")
                                elif not uploaded:
                                    st.error("Select a file.")
                                else:
                                    path = save_upload(
                                        uploaded,
                                        f"client_{client['id']}"
                                    )

                                    db.add_document(
                                        org_id,
                                        doc_name.strip(),
                                        path,
                                        client_id=client["id"]
                                    )

                                    st.success("Document uploaded.")
                                    st.rerun()


# ============================================================
# MATTERS & TASKS
# ============================================================

elif menu == "Matters & Tasks":

    st.header("Matters & Tasks")

    st.markdown("""
    <div class="nx-soft">
        Matter Type tells Nexora what kind of legal file this is.
        Task Type / Service tells Nexora what work is being done inside the matter.
    </div>
    """, unsafe_allow_html=True)

    tab_open, tab_directory, tab_work = st.tabs(
        ["➕ Open Matter", "Matter Directory", "Work on Matter"]
    )

    with tab_open:

        clients = db.list_clients(org_id)
        matter_types = db.list_matter_types(org_id)

        if not clients:
            st.warning("Register a client first.")
        else:
            with st.form("open_matter_form"):

                client_map = {
                    f"{c['client_number']} — {c['name']}": c
                    for c in clients
                }

                selected_client = client_map[
                    st.selectbox("Client", list(client_map.keys()))
                ]

                type_map = {
                    m["name"]: m
                    for m in matter_types
                }

                selected_type = type_map[
                    st.selectbox("Matter Type", list(type_map.keys()))
                ]

                title = st.text_input("Matter Title")
                priority = st.selectbox(
                    "Priority",
                    ["Low", "Normal", "High", "Urgent"],
                    index=1
                )
                particulars = st.text_area("Matter Particulars")

                if st.form_submit_button(
                    "Open Matter",
                    use_container_width=True
                ):
                    if not title.strip():
                        st.error("Matter title is required.")
                    else:
                        try:
                            matter_id, matter_number = db.create_matter(
                                org_id,
                                selected_client["id"],
                                selected_type["id"],
                                title.strip(),
                                priority,
                                particulars.strip(),
                                user_id
                            )

                            st.success(
                                f"Matter {matter_number} successfully opened."
                            )
                        except Exception as e:
                            st.error(f"Could not open matter: {e}")

    with tab_directory:

        matters = db.list_matters(org_id)

        if not matters:
            st.info("No matters opened yet.")
        else:
            rows = [
                {
                    "Matter Number": m["matter_number"],
                    "Client": m["client_name"],
                    "Matter Type": m.get("matter_type"),
                    "Title": m["title"],
                    "Status": m["status"],
                    "Priority": m["priority"]
                }
                for m in matters
            ]

            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True
            )

    with tab_work:

        matters = db.list_matters(org_id)

        if not matters:
            st.info("No matters available.")
        else:
            matter_map = {
                f"{m['matter_number']} — {m['title']}": m
                for m in matters
            }

            selected_matter = matter_map[
                st.selectbox("Select Matter", list(matter_map.keys()))
            ]

            st.subheader(selected_matter["title"])
            st.write(f"**Matter Number:** `{selected_matter['matter_number']}`")
            st.write(f"**Client:** {selected_matter['client_name']}")
            st.write(f"**Matter Type:** {selected_matter.get('matter_type')}")
            st.write(f"**Matter Status:** {selected_matter['status']}")
            if selected_matter.get("closed_at"):
                st.write(f"**Closed At:** {selected_matter['closed_at']}")

            action_col1, action_col2 = st.columns([1, 3])

            with action_col1:
                if selected_matter["status"] != "Closed":
                    if st.button(
                        "🔒 Close Matter",
                        key=f"work_close_matter_{selected_matter['id']}",
                        use_container_width=True
                    ):
                        try:
                            db.close_matter(
                                org_id,
                                selected_matter["id"]
                            )
                            st.success(
                                "Matter closed. Existing tasks, documents and invoices remain available."
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))
                else:
                    if st.button(
                        "🔓 Reopen Matter",
                        key=f"work_reopen_matter_{selected_matter['id']}",
                        use_container_width=True
                    ):
                        try:
                            db.reopen_matter(
                                org_id,
                                selected_matter["id"]
                            )
                            st.success("Matter reopened.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

            with action_col2:
                if selected_matter["status"] == "Closed":
                    st.info(
                        "This matter is closed. Historical tasks, documents and billing remain visible."
                    )

            st.divider()

            services = db.list_services(org_id)

            if selected_matter["status"] != "Closed":

                st.subheader("Start a Task")

                if not services:
                    st.warning(
                        "No Task Types / Services have been configured. "
                        "Ask the Admin to add them first."
                    )
                else:
                    service_map = {
                        s["name"]: s
                        for s in services
                    }

                    with st.form("start_task_form"):

                        selected_service = service_map[
                            st.selectbox(
                                "Task Type / Service",
                                list(service_map.keys())
                            )
                        ]

                        task_title = st.text_input(
                            "Task Description",
                            value=selected_service["name"]
                        )

                        tat_days = st.number_input(
                            "TAT (Days)",
                            min_value=1,
                            value=5
                        )

                        due_date = st.date_input(
                            "Due Date",
                            value=date.today() + timedelta(days=5)
                        )

                        if st.form_submit_button("Start Task"):

                            try:
                                task_id, task_number = db.create_task(
                                    org_id,
                                    selected_matter["id"],
                                    user_id,
                                    selected_service["id"],
                                    task_title.strip(),
                                    tat_days,
                                    str(due_date)
                                )

                                st.success(
                                    f"Task {task_number} started."
                                )
                                st.rerun()

                            except Exception as e:
                                st.error(str(e))

            st.divider()
            st.subheader("Tasks in this Matter")

            tasks = db.list_tasks(
                org_id,
                matter_id=selected_matter["id"]
            )

            if not tasks:
                st.info("No tasks started yet.")
            else:
                for task in tasks:
                    with st.container(border=True):
                        st.write(f"**Task Number:** `{task['task_number']}`")
                        st.write(f"**Client:** {task['client_name']}")
                        st.write(f"**Matter:** {task['matter_number']}")
                        st.write(
                            f"**Task Type / Service:** {task['service_name']}"
                        )
                        st.write(f"**Task:** {task['title']}")
                        st.write(
                            f"**Practitioner:** {task['practitioner_name']} "
                            f"({task['practitioner_type']})"
                        )
                        st.write(f"**Status:** {task['status']}")
                        st.write(f"**Billing:** {task['billing_status']}")
                        attention = (
                            task_attention_label(task.get("due_date"))
                            if task["status"] != "Complete"
                            else None
                        )
                        if attention:
                            st.error(attention)
                        else:
                            st.caption(due_text(task["due_date"]))

                        if task["status"] == "Complete":
                            st.success(
                                f"Completed | Quantity: {task['billable_quantity']} | "
                                f"Rate: {money(task['rate_applied'])} | "
                                f"Fees: {money(task['billable_amount'])} | "
                                f"Disbursement: {money(task.get('disbursement_amount'))} | "
                                f"Line Total: {money(float(task['billable_amount'] or 0) + float(task.get('disbursement_amount') or 0))}"
                            )

                            if task.get("completion_notes"):
                                st.caption(task["completion_notes"])

                        elif (
                            int(task["user_id"]) == int(user_id)
                            or admin_access()
                        ):
                            with st.expander(
                                "Complete Task & Record Billable Time"
                            ):
                                with st.form(
                                    f"complete_task_{task['id']}"
                                ):

                                    quantity = st.number_input(
                                        "Billable Hours / Units",
                                        min_value=0.01,
                                        value=1.0,
                                        step=0.25
                                    )

                                    disbursement_amount = st.number_input(
                                        "Disbursement Amount (R)",
                                        min_value=0.0,
                                        value=0.0,
                                        step=50.0,
                                        help="Enter external costs incurred for this task, if any. Explain the disbursement in the completion notes."
                                    )

                                    completion_notes = st.text_area(
                                        "Completion Notes / Comment",
                                        help="Include the disbursement explanation here where applicable."
                                    )

                                    if st.form_submit_button("Complete Task"):
                                        try:
                                            result = db.complete_task(
                                                org_id,
                                                task["id"],
                                                quantity,
                                                completion_notes,
                                                disbursement_amount
                                            )

                                            st.success(
                                                f"Task completed using {result['rate_source']}. "
                                                f"Rate: {money(result['rate'])} | "
                                                f"Fees: {money(result['amount'])} | "
                                                f"Disbursement: {money(result['disbursement_amount'])} | "
                                                f"Total: {money(result['line_total'])}"
                                            )
                                            st.rerun()

                                        except Exception as e:
                                            st.error(str(e))


# ============================================================
# DOCUMENTS
# ============================================================

elif menu == "Documents":

    st.header("Documents")

    st.markdown("""
    <div class="nx-soft">
        Store client and matter documents here. Nexora does not require a client letterhead.
    </div>
    """, unsafe_allow_html=True)

    matters = db.list_matters(org_id)

    if not matters:
        st.info("Open a matter first.")
    else:
        matter_map = {
            f"{m['matter_number']} — {m['title']}": m
            for m in matters
        }

        matter = matter_map[
            st.selectbox("Matter", list(matter_map.keys()))
        ]

        docs = db.get_documents_for_matter(
            org_id,
            matter["id"]
        )

        if docs:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Document": d["document_name"],
                        "File": os.path.basename(d["file_path"]),
                        "Created": d["created_at"]
                    }
                    for d in docs
                ]),
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("No documents uploaded for this matter.")

        with st.form("matter_document_form"):

            document_name = st.text_input("Document Name / Type")
            uploaded = st.file_uploader(
                "Choose File",
                type=["pdf", "docx", "png", "jpg", "jpeg"]
            )

            if st.form_submit_button("Upload Document"):

                if not document_name.strip():
                    st.error("Document name is required.")
                elif not uploaded:
                    st.error("Choose a file.")
                else:
                    path = save_upload(
                        uploaded,
                        f"matter_{matter['id']}"
                    )

                    db.add_document(
                        org_id,
                        document_name.strip(),
                        path,
                        matter_id=matter["id"],
                        client_id=matter["client_id"]
                    )

                    st.success("Document uploaded.")
                    st.rerun()


# ============================================================
# COMMUNICATIONS
# ============================================================

elif menu == "Communications":

    st.header("Communications")

    st.markdown("""
    <div class="nx-soft">
        Record calls/WhatsApp activity and queue emails for clients.
    </div>
    """, unsafe_allow_html=True)

    tab_calls, tab_email = st.tabs(
        ["Call / WhatsApp Logs", "Email Queue"]
    )

    clients = db.list_clients(org_id)

    with tab_calls:

        if not clients:
            st.info("No clients registered.")
        else:
            client_map = {
                f"{c['client_number']} — {c['name']}": c
                for c in clients
            }

            with st.form("call_form"):
                client = client_map[
                    st.selectbox(
                        "Client",
                        list(client_map.keys())
                    )
                ]

                status = st.selectbox(
                    "Status",
                    [
                        "Completed",
                        "Missed",
                        "Voicemail",
                        "WhatsApp Message"
                    ]
                )

                details = st.text_area("Notes")

                if st.form_submit_button("Save Communication"):
                    db.save_call_log(
                        org_id,
                        datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        client.get("phone") or "",
                        client["name"],
                        details.strip(),
                        status
                    )

                    st.success("Communication logged.")
                    st.rerun()

        call_logs = db.fetch_call_logs(org_id)

        if not call_logs.empty:
            st.dataframe(
                call_logs,
                use_container_width=True,
                hide_index=True
            )

    with tab_email:

        if not clients:
            st.info("No clients registered.")
        else:
            client_map = {
                f"{c['client_number']} — {c['name']}": c
                for c in clients
            }

            with st.form("email_form"):
                client = client_map[
                    st.selectbox(
                        "Client",
                        list(client_map.keys())
                    )
                ]

                recipient = st.text_input(
                    "Recipient Email",
                    value=client.get("email") or ""
                )

                subject = st.text_input("Subject")
                body = st.text_area("Message", height=180)

                if st.form_submit_button("Queue Email"):

                    if not recipient.strip():
                        st.error("Recipient email is required.")
                    elif not subject.strip():
                        st.error("Subject is required.")
                    elif not body.strip():
                        st.error("Message is required.")
                    else:
                        db.queue_email(
                            org_id,
                            recipient.strip(),
                            subject.strip(),
                            body.strip(),
                            datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                        )

                        st.success("Email queued.")
                        st.rerun()

        email_logs = db.fetch_email_logs(org_id)

        if not email_logs.empty:
            st.dataframe(
                email_logs,
                use_container_width=True,
                hide_index=True
            )


# ============================================================
# BILLING
# ============================================================

elif menu == "Billing & Invoices":

    st.header("Billing & Invoices")

    st.markdown("""
    <div class="nx-soft">
        Completed tasks remain Unbilled until selected for an invoice.
        General clients use Service × Practitioner Type fees.
        SLA clients use that client's practitioner-type SLA rate.
    </div>
    """, unsafe_allow_html=True)

    summary = db.get_billing_summary(org_id)

    c1, c2, c3, c4 = st.columns(4)

    c1.metric("Unbilled Tasks", summary["unbilled_tasks"])
    c2.metric("Draft Invoices", summary["draft_invoices"])
    c3.metric("Issued Invoices", summary["issued_invoices"])
    c4.metric(
        "Outstanding",
        money(summary["total_outstanding"])
    )

    tab_create, tab_register = st.tabs(
        ["➕ Create Invoice", "Invoice Register"]
    )

    with tab_create:

        clients = db.list_clients(org_id)

        if not clients:
            st.info("Register a client first.")
        else:
            client_map = {
                f"{c['client_number']} — {c['name']}": c
                for c in clients
            }

            client = client_map[
                st.selectbox(
                    "Client",
                    list(client_map.keys())
                )
            ]

            billable = db.get_client_unbilled_billing_items(
                org_id,
                client["id"]
            )

            tasks = billable["tasks"]

            if not tasks:
                st.info(
                    "This client has no completed unbilled tasks."
                )
            else:
                with st.form("new_invoice_form"):

                    invoice_date = st.date_input(
                        "Invoice Date",
                        value=date.today()
                    )

                    due_date = st.date_input(
                        "Due Date",
                        value=date.today() + timedelta(days=30)
                    )

                    vat_rate = st.number_input(
                        "VAT Rate (%)",
                        min_value=0.0,
                        value=0.0,
                        step=0.5
                    )

                    payment_terms = st.text_input(
                        "Payment Terms",
                        value=(
                            db.get_organization(org_id)
                            .get("invoice_payment_terms")
                            or "30 days"
                        )
                    )

                    notes = st.text_area("Invoice Notes")

                    selected_task_ids = []

                    st.markdown("### Completed Tasks")

                    for task in tasks:
                        include = st.checkbox(
                            (
                                f"{task['task_number']} | "
                                f"{task['matter_number']} | "
                                f"{task['service_name']} | "
                                f"{task['practitioner_name']} | "
                                f"Fees {money(task['billable_amount'])} | "
                                f"Disbursement {money(task.get('disbursement_amount'))}"
                            ),
                            key=f"invoice_task_{task['id']}"
                        )

                        if include:
                            selected_task_ids.append(task["id"])

                    if st.form_submit_button(
                        "Create Invoice",
                        use_container_width=True
                    ):

                        if not selected_task_ids:
                            st.error(
                                "Select at least one completed task."
                            )
                        else:
                            try:
                                invoice_id = db.create_invoice(
                                    org_id,
                                    client["id"],
                                    str(invoice_date),
                                    str(due_date),
                                    user_id,
                                    notes.strip(),
                                    vat_rate,
                                    payment_terms.strip()
                                )

                                for task_id in selected_task_ids:
                                    db.add_task_to_invoice(
                                        org_id,
                                        invoice_id,
                                        task_id
                                    )

                                invoice = db.get_invoice(
                                    org_id,
                                    invoice_id
                                )

                                st.success(
                                    f"Invoice {invoice['invoice_number']} created successfully."
                                )
                                st.rerun()

                            except Exception as e:
                                st.error(
                                    f"Could not create invoice: {e}"
                                )

    with tab_register:

        invoices = db.list_invoices(org_id)

        if not invoices:
            st.info("No invoices created yet.")
        else:
            for invoice in invoices:
                with st.container(border=True):

                    paid = db.get_invoice_amount_paid(
                        org_id,
                        invoice["id"]
                    )

                    outstanding = (
                        float(invoice["total_amount"] or 0)
                        - paid
                    )

                    st.write(
                        f"**Invoice:** `{invoice['invoice_number']}`"
                    )
                    st.write(
                        f"**Client:** {invoice['client_name']}"
                    )
                    st.write(
                        f"**Status:** {invoice['status']}"
                    )
                    st.write(
                        f"**Professional Fees:** {money(invoice.get('fees_subtotal'))}"
                    )
                    st.write(
                        f"**Disbursements:** {money(invoice.get('disbursement_total'))}"
                    )
                    st.write(
                        f"**Total:** {money(invoice['total_amount'])}"
                    )
                    st.write(
                        f"**Paid:** {money(paid)}"
                    )
                    st.write(
                        f"**Outstanding:** {money(outstanding)}"
                    )

                    items = db.list_invoice_items(
                        org_id,
                        invoice["id"]
                    )

                    if items:
                        st.dataframe(
                            pd.DataFrame([
                                {
                                    "Matter": x["matter_number"],
                                    "Description": x["description"],
                                    "Practitioner Type":
                                        x["practitioner_type"],
                                    "Quantity": x["quantity"],
                                    "Rate": x["rate"],
                                    "Fees": x["amount"],
                                    "Disbursement": x.get("disbursement_amount") or 0,
                                    "Line Total": float(x.get("amount") or 0) + float(x.get("disbursement_amount") or 0)
                                }
                                for x in items
                            ]),
                            use_container_width=True,
                            hide_index=True
                        )

                        try:
                            invoice_pdf = generate_invoice_pdf(
                                org_id,
                                invoice["id"]
                            )

                            st.download_button(
                                label="📄 Download Invoice PDF",
                                data=invoice_pdf,
                                file_name=f"{invoice['invoice_number']}.pdf",
                                mime="application/pdf",
                                key=f"download_invoice_{invoice['id']}",
                                use_container_width=True
                            )

                        except Exception as pdf_error:
                            st.error(
                                f"Invoice PDF could not be generated: {pdf_error}"
                            )

                    if invoice["status"] == "Draft":
                        if st.button(
                            "Issue Invoice",
                            key=f"issue_{invoice['id']}"
                        ):
                            try:
                                db.issue_invoice(
                                    org_id,
                                    invoice["id"]
                                )
                                st.success("Invoice issued.")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

                    if invoice["status"] not in (
                        "Paid",
                        "Void"
                    ):
                        with st.expander("Record Payment"):
                            with st.form(
                                f"payment_{invoice['id']}"
                            ):
                                amount = st.number_input(
                                    "Payment Amount",
                                    min_value=0.01,
                                    value=(
                                        outstanding
                                        if outstanding > 0
                                        else 0.01
                                    )
                                )

                                reference = st.text_input(
                                    "Reference"
                                )

                                if st.form_submit_button(
                                    "Record Payment"
                                ):
                                    try:
                                        db.add_invoice_payment(
                                            org_id,
                                            invoice["id"],
                                            amount,
                                            str(date.today()),
                                            reference.strip(),
                                            "",
                                            user_id
                                        )

                                        st.success(
                                            "Payment recorded."
                                        )
                                        st.rerun()

                                    except Exception as e:
                                        st.error(str(e))

                    if invoice["status"] not in (
                        "Paid",
                        "Void"
                    ):
                        if st.button(
                            "Void Invoice",
                            key=f"void_{invoice['id']}"
                        ):
                            try:
                                db.void_invoice(
                                    org_id,
                                    invoice["id"]
                                )

                                st.success(
                                    "Invoice voided. Underlying tasks returned to Unbilled."
                                )
                                st.rerun()

                            except Exception as e:
                                st.error(str(e))


# ============================================================
# ACTUARIAL
# ============================================================

elif menu == "Actuarial & Damages":

    st.header("Actuarial & Damages Calculator")

    st.markdown("""
    <div class="nx-soft">
        Simple working estimate for loss of earnings, general damages and medical expenses.
        It is not a substitute for a formal actuarial report.
    </div>
    """, unsafe_allow_html=True)

    matters = db.list_matters(org_id)

    if not matters:
        st.info("Open a matter first.")
    else:
        matter_map = {
            f"{m['matter_number']} — {m['title']}": m
            for m in matters
        }

        matter = matter_map[
            st.selectbox(
                "Matter",
                list(matter_map.keys())
            )
        ]

        with st.form("quantum_form"):

            c1, c2 = st.columns(2)

            with c1:
                current_age = st.number_input(
                    "Current Age",
                    min_value=1,
                    max_value=100,
                    value=35
                )

                retirement_age = st.number_input(
                    "Retirement Age",
                    min_value=1,
                    max_value=100,
                    value=65
                )

                annual_past = st.number_input(
                    "Annual Earnings — Past (R)",
                    min_value=0.0,
                    value=240000.0
                )

                years_past = st.number_input(
                    "Years Since Accident",
                    min_value=0.0,
                    value=3.0,
                    step=0.5
                )

            with c2:
                annual_future = st.number_input(
                    "Projected Annual Earnings — Future (R)",
                    min_value=0.0,
                    value=300000.0
                )

                residual = st.number_input(
                    "Residual Earning Capacity (R)",
                    min_value=0.0,
                    value=120000.0
                )

                general_damages = st.number_input(
                    "General Damages (R)",
                    min_value=0.0,
                    value=500000.0
                )

                medical = st.number_input(
                    "Medical Expenses (R)",
                    min_value=0.0,
                    value=150000.0
                )

            if st.form_submit_button(
                "Compute Quantum",
                use_container_width=True
            ):

                past_loss = max(
                    annual_past * years_past
                    - residual * years_past,
                    0
                )

                remaining_years = max(
                    retirement_age
                    - current_age
                    - years_past,
                    0
                )

                future_loss = max(
                    annual_future * remaining_years
                    - residual * remaining_years,
                    0
                )

                total = (
                    past_loss
                    + future_loss
                    + general_damages
                    + medical
                )

                st.success(
                    f"Estimated Claim Value for "
                    f"{matter['matter_number']}: "
                    f"{money(total)}"
                )


# ============================================================
# AI
# ============================================================

elif menu == "AI Assistant":

    st.header("Nexora Legal AI Assistant")

    st.markdown("""
    <div class="nx-soft">
        Use this tab for drafting assistance, summaries and internal practice support.
    </div>
    """, unsafe_allow_html=True)

    # The actual OpenAI key is NOT stored in this file.
    # Nexora reads it from the OPENAI_API_KEY environment variable.
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    query = st.text_area(
        "Question / Instruction",
        height=180
    )

    if st.button("Generate AI Response"):

        if not query.strip():
            st.warning("Enter a question first.")

        elif not api_key:
            st.warning(
                "OPENAI_API_KEY is not configured. "
                "Set it in PowerShell before starting Nexora."
            )

        elif not OpenAI:
            st.warning(
                "The OpenAI Python package is not installed."
            )

        else:
            try:
                client = OpenAI(api_key=api_key)

                prompt = f"""
You are Nexora Legal AI.

You are assisting {user_name}, a {attorney_level}, at
{firm_name}, Nexora firm number {firm_number}.

You are an internal legal-practice assistant.

Rules:
- Be professional and practical.
- Never disclose information belonging to another law firm.
- Do not invent client or matter facts.
- Treat legal drafting as a draft for practitioner review.
- If information is missing, say what is missing.
- Do not claim that you performed an action inside Nexora.

USER REQUEST:

{query}
"""

                response = client.responses.create(
                    model="gpt-5.6-luna",
                    input=prompt
                )

                st.markdown(response.output_text)

            except Exception as e:
                st.error(
                    f"AI service error: {e}"
                )


# ============================================================
# ADMIN
# ============================================================

elif menu == "Admin & Firm Settings":

    if not admin_access():
        st.error(
            "Only the Firm Administrator can access this page."
        )
        st.stop()

    st.header("Admin & Firm Settings")

    st.markdown("""
    <div class="nx-soft">
        Recommended setup order:
        Practitioner Types → Practitioners → Matter Types → Task Types / Services → General Fees → Firm Details.
    </div>
    """, unsafe_allow_html=True)

    (
        tab_types,
        tab_people,
        tab_matter_types,
        tab_services,
        tab_fees,
        tab_firm,
    ) = st.tabs(
        [
            "1️⃣ Practitioner Types",
            "2️⃣ Practitioners",
            "3️⃣ Matter Types",
            "4️⃣ Task Types / Services",
            "5️⃣ General Fees",
            "6️⃣ Firm Details"
        ]
    )

    with tab_types:

        st.subheader("Practitioner Types")

        st.caption(
            "Employee/practitioner classifications used by this firm."
        )

        with st.form("add_practitioner_type_form"):

            type_name = st.text_input("Practitioner Type")
            description = st.text_area("Description")

            if st.form_submit_button(
                "Add Practitioner Type"
            ):
                try:
                    db.add_practitioner_type(
                        org_id,
                        type_name.strip(),
                        description.strip()
                    )
                    st.success("Practitioner type added.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        types = db.list_practitioner_types(
            org_id,
            active_only=False
        )

        if types:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Practitioner Type": t["name"],
                        "Description": t.get("description") or "",
                        "Active": "Yes" if int(t["active"]) else "No"
                    }
                    for t in types
                ]),
                use_container_width=True,
                hide_index=True
            )

    with tab_people:

        st.subheader("Practitioners")

        st.caption(
            "Actual people who can log in to the firm."
        )

        types = db.list_practitioner_types(org_id)

        if not types:
            st.warning(
                "Add at least one Practitioner Type first."
            )
        else:
            type_map = {
                t["name"]: t
                for t in types
            }

            with st.form("add_practitioner_form"):

                name = st.text_input("Full Name")
                cell = st.text_input("Cellphone Number")
                role = st.selectbox(
                    "System Role",
                    ["Lawyer", "Admin"]
                )

                pt_name = st.selectbox(
                    "Practitioner Type",
                    list(type_map.keys())
                )

                if st.form_submit_button(
                    "Add Practitioner"
                ):
                    try:
                        pt = type_map[pt_name]

                        db.create_user_with_credentials(
                            org_id,
                            name.strip(),
                            cell.strip(),
                            role,
                            pt_name,
                            pt["id"]
                        )

                        st.success(
                            f"{name.strip()} successfully registered to {firm_name}."
                        )
                        st.rerun()

                    except Exception as e:
                        st.error(str(e))

        users = db.list_users(org_id)

        if users:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Name": u["name"],
                        "Cell / Login": u["cell"],
                        "Role": u["role"],
                        "Practitioner Type":
                            u.get("practitioner_type")
                            or u.get("attorney_level"),
                        "Active":
                            "Yes"
                            if int(u["active"])
                            else "No"
                    }
                    for u in users
                ]),
                use_container_width=True,
                hide_index=True
            )

    with tab_matter_types:

        st.subheader("Matter Types")

        st.caption(
            "Common legal matter types are preloaded. Add more if your firm needs them."
        )

        with st.form("add_matter_type_form"):

            matter_type_name = st.text_input(
                "New Matter Type"
            )

            if st.form_submit_button(
                "Add Matter Type"
            ):
                try:
                    db.add_matter_type(
                        org_id,
                        matter_type_name.strip()
                    )
                    st.success("Matter type added.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        matter_types = db.list_matter_types(
            org_id,
            active_only=False
        )

        st.dataframe(
            pd.DataFrame([
                {
                    "Matter Type": m["name"],
                    "Active":
                        "Yes"
                        if int(m["active"])
                        else "No"
                }
                for m in matter_types
            ]),
            use_container_width=True,
            hide_index=True
        )

    with tab_services:

        st.subheader("Task Types / Services")

        st.caption(
            "The firm defines the actual kinds of legal work performed."
        )

        with st.form("add_service_form"):

            service_name = st.text_input(
                "Task Type / Service Name"
            )

            description = st.text_area(
                "Description"
            )

            default_unit = st.selectbox(
                "Default Billing Unit",
                [
                    "Hour",
                    "Quarter Hour",
                    "Half Hour",
                    "Per Consultation",
                    "Per Document",
                    "Per Matter",
                    "Fixed Fee",
                    "Other"
                ]
            )

            if st.form_submit_button(
                "Add Task Type / Service"
            ):
                try:
                    db.add_service(
                        org_id,
                        service_name.strip(),
                        description.strip(),
                        default_unit
                    )

                    st.success(
                        "Task Type / Service added."
                    )
                    st.rerun()

                except Exception as e:
                    st.error(str(e))

        services = db.list_services(
            org_id,
            active_only=False
        )

        if services:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Task Type / Service": s["name"],
                        "Description": s.get("description") or "",
                        "Default Unit": s["default_unit"],
                        "Active":
                            "Yes"
                            if int(s["active"])
                            else "No"
                    }
                    for s in services
                ]),
                use_container_width=True,
                hide_index=True
            )

    with tab_fees:

        st.subheader("General Client Fee Schedule")

        st.caption(
            "Used only for General clients. SLA client rates are captured on that client's registration."
        )

        services = db.list_services(org_id)
        types = db.list_practitioner_types(org_id)

        if not services:
            st.warning(
                "Add Task Types / Services first."
            )

        elif not types:
            st.warning(
                "Add Practitioner Types first."
            )

        else:
            service_map = {
                s["name"]: s
                for s in services
            }

            selected_service = service_map[
                st.selectbox(
                    "Task Type / Service",
                    list(service_map.keys())
                )
            ]

            unit = st.selectbox(
                "Billing Unit",
                [
                    "Hour",
                    "Quarter Hour",
                    "Half Hour",
                    "Per Consultation",
                    "Per Document",
                    "Per Matter",
                    "Fixed Fee",
                    "Other"
                ]
            )

            with st.form("general_fee_form"):

                cols = st.columns(2)
                fees = {}

                for idx, pt in enumerate(types):
                    with cols[idx % 2]:
                        fees[pt["id"]] = st.number_input(
                            f"{pt['name']} Fee (R)",
                            min_value=0.0,
                            value=0.0,
                            step=100.0,
                            key=(
                                f"general_fee_"
                                f"{selected_service['id']}_"
                                f"{pt['id']}"
                            )
                        )

                if st.form_submit_button(
                    "Save General Fees"
                ):
                    try:
                        for pt_id, fee in fees.items():
                            db.save_service_fee(
                                org_id,
                                selected_service["id"],
                                pt_id,
                                fee,
                                unit
                            )

                        st.success(
                            "General fee schedule saved."
                        )
                        st.rerun()

                    except Exception as e:
                        st.error(str(e))

        schedule = db.list_service_fees(org_id)

        if schedule:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Task Type / Service": f["service_name"],
                        "Practitioner Type": f["practitioner_type"],
                        "Fee": f["fee"],
                        "Unit": f["unit"]
                    }
                    for f in schedule
                ]),
                use_container_width=True,
                hide_index=True
            )

    with tab_firm:

        st.subheader("Firm / Invoice Details")

        st.caption(
            "These details are used for the firm's generated invoice header and payment information."
        )

        firm = db.get_organization(org_id)

        with st.form("firm_details_form"):

            address = st.text_area(
                "Firm Address",
                value=firm.get("address") or ""
            )

            email = st.text_input(
                "Firm Email",
                value=firm.get("email") or ""
            )

            phone = st.text_input(
                "Firm Phone",
                value=firm.get("phone") or ""
            )

            website = st.text_input(
                "Website",
                value=firm.get("website") or ""
            )

            registration_number = st.text_input(
                "Registration Number",
                value=firm.get("registration_number") or ""
            )

            vat_number = st.text_input(
                "VAT Number",
                value=firm.get("vat_number") or ""
            )

            bank_name = st.text_input(
                "Bank Name",
                value=firm.get("bank_name") or ""
            )

            bank_account_name = st.text_input(
                "Bank Account Name",
                value=firm.get("bank_account_name") or ""
            )

            bank_account_number = st.text_input(
                "Bank Account Number",
                value=firm.get("bank_account_number") or ""
            )

            bank_branch_code = st.text_input(
                "Bank Branch Code",
                value=firm.get("bank_branch_code") or ""
            )

            invoice_payment_terms = st.text_input(
                "Default Invoice Payment Terms",
                value=firm.get("invoice_payment_terms") or "30 days"
            )

            if st.form_submit_button(
                "Save Firm Details"
            ):

                db.update_organization_details(
                    org_id,
                    name=firm_name,
                    address=address.strip(),
                    email=email.strip(),
                    phone=phone.strip(),
                    website=website.strip(),
                    registration_number=registration_number.strip(),
                    vat_number=vat_number.strip(),
                    bank_name=bank_name.strip(),
                    bank_account_name=bank_account_name.strip(),
                    bank_account_number=bank_account_number.strip(),
                    bank_branch_code=bank_branch_code.strip(),
                    invoice_payment_terms=invoice_payment_terms.strip()
                )

                st.success("Firm details saved.")
                st.rerun()
