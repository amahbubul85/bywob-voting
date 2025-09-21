# streamlit_app.py
# BYWOB Online Voting — Streamlit + Google Sheets
# - Auto-creates required worksheets with headers (meta, voters, candidates, votes)
# - One-time token voting
# - Election window (start/end in UTC): idle | ongoing | ended | published
# - Block votes outside window, publish/declare results
# - Token generator (no hard max)
# - Live tally, CSV export
# - Archive & clear votes

import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="centered")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + Google Sheets • Secret ballot with one-time tokens")

# --------------------------------------------------------------------------------------
# Secrets & Google Sheets connection
# --------------------------------------------------------------------------------------
def _require_secrets():
    if "gcp_service_account" not in st.secrets:
        st.error("Secrets missing: gcp_service_account. App → Settings → Secrets এ service account JSON + SHEET_ID দিন।")
        st.stop()

_require_secrets()

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
client = gspread.authorize(creds)

try:
    SHEET_ID = st.secrets["gcp_service_account"]["SHEET_ID"]
    sheet = client.open_by_key(SHEET_ID)
except Exception as e:
    st.error(f"❌ Google Sheet ওপেন করা যায়নি: {e}")
    st.stop()

# --------------------------------------------------------------------------------------
# Worksheet ensure/create helpers
# --------------------------------------------------------------------------------------
def ensure_ws(title: str, headers: list[str], rows: int = 100, cols: int = 10):
    """Return worksheet; create if missing and set headers."""
    try:
        ws = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=title, rows=rows, cols=cols)
        # write headers in row 1
        rng = f"A1:{chr(64+len(headers))}1"
        ws.update(values=[headers], range_name=rng)
    return sheet.worksheet(title)

# Create or fetch all required sheets
meta_ws        = ensure_ws("meta", ["key", "value"], rows=20, cols=2)
voters_ws      = ensure_ws("voters", ["name", "email", "token", "used", "used_at"], rows=2000, cols=5)
candidates_ws  = ensure_ws("candidates", ["position", "candidate"], rows=500, cols=2)
votes_ws       = ensure_ws("votes", ["position", "candidate", "timestamp"], rows=5000, cols=3)

# --------------------------------------------------------------------------------------
# Meta helpers (status & schedule)
# --------------------------------------------------------------------------------------
def now_utc():
    return datetime.now(timezone.utc)

def meta_get_all() -> dict:
    recs = meta_ws.get_all_records()
    return {r.get("key"): r.get("value") for r in recs if r.get("key")}

def meta_set(key: str, value: str):
    recs = meta_ws.get_all_records()
    # find existing key row (1-based with header)
    for i, r in enumerate(recs, start=2):
        if r.get("key") == key:
            meta_ws.update_cell(i, 2, value)
            return
    # append new
    meta_ws.append_row([key, value], value_input_option="RAW")

# Set defaults if first run
_meta = meta_get_all()
if "status" not in _meta:     meta_set("status", "idle")         # idle | ongoing | ended | published
if "name" not in _meta:       meta_set("name", "")
if "start_utc" not in _meta:  meta_set("start_utc", "")
if "end_utc" not in _meta:    meta_set("end_utc", "")
if "published" not in _meta:  meta_set("published", "FALSE")

def is_voting_open() -> bool:
    m = meta_get_all()
    if m.get("status", "idle") != "ongoing":
        return False
    try:
        start = m.get("start_utc", "")
        end   = m.get("end_utc", "")
        start_dt = datetime.fromisoformat(start) if start else None
        end_dt   = datetime.fromisoformat(end) if end else None
    except Exception:
        return False
    now = now_utc()
    if start_dt and now < start_dt.astimezone(timezone.utc):
        return False
    if end_dt and now > end_dt.astimezone(timezone.utc):
        meta_set("status", "ended")  # auto-close
        return False
    return True

# --------------------------------------------------------------------------------------
# Cached loaders
# --------------------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_voters_df():
    df = pd.DataFrame(voters_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["name", "email", "token", "used", "used_at"])
    # normalize
    for col in ["name", "email", "token", "used", "used_at"]:
        if col not in df.columns:
            df[col] = ""
    df["token"] = df["token"].astype(str).str.strip()
    df["used_bool"] = df["used"].astype(str).str.strip().str.lower().isin(["true", "1", "yes"])
    return df[["name", "email", "token", "used", "used_at", "used_bool"]]

@st.cache_data(show_spinner=False)
def load_candidates_df():
    df = pd.DataFrame(candidates_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["position", "candidate"])
    for col in ["position", "candidate"]:
        if col not in df.columns: df[col] = ""
    df["position"] = df["position"].astype(str).str.strip()
    df["candidate"] = df["candidate"].astype(str).str.strip()
    df = df[(df["position"] != "") & (df["candidate"] != "")]
    return df[["position", "candidate"]]

@st.cache_data(show_spinner=False)
def load_votes_df():
    df = pd.DataFrame(votes_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["position", "candidate", "timestamp"])
    return df[["position", "candidate", "timestamp"]]

def clear_caches():
    load_voters_df.clear(); load_candidates_df.clear(); load_votes_df.clear()

# --------------------------------------------------------------------------------------
# Sheet operations
# --------------------------------------------------------------------------------------
def mark_token_used(voters_df: pd.DataFrame, token: str):
    t = str(token).strip()
    m = voters_df[voters_df["token"] == t]
    if m.empty: return
    row_idx = m.index[0] + 2  # header offset
    voters_ws.update_cell(row_idx, 4, "TRUE")                    # used
    voters_ws.update_cell(row_idx, 5, now_utc().isoformat())     # used_at
    load_voters_df.clear()

def append_vote(position: str, candidate: str):
    votes_ws.append_row([position, candidate, now_utc().isoformat()], value_input_option="RAW")
    load_votes_df.clear()

def generate_tokens(n: int, prefix: str):
    import secrets, string
    alpha = string.ascii_uppercase + string.digits
    rows = []
    for _ in range(n):
        tok = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        rows.append(["", "", tok, "FALSE", ""])
    if rows:
        votes = voters_ws.append_rows(rows, value_input_option="RAW")
        load_voters_df.clear()

def archive_and_clear_votes(election_name: str | None):
    rows = votes_ws.get_all_records()
    if not rows:
        return "no_votes"
    ts = now_utc().strftime("%Y%m%dT%H%M%SZ")
    safe = (election_name or "election").replace(" ", "_")[:20]
    title = f"votes_archive_{safe}_{ts}"
    new_ws = sheet.add_worksheet(title=title, rows=len(rows)+5, cols=3)
    new_ws.update(values=[["position","candidate","timestamp"]], range_name="A1:C1")
    new_ws.append_rows([[r["position"], r["candidate"], r["timestamp"]] for r in rows], value_input_option="RAW")
    votes_ws.clear()
    votes_ws.append_row(["position","candidate","timestamp"], value_input_option="RAW")
    load_votes_df.clear()
    return title

def results_df():
    df = load_votes_df()
    if df.empty:
        return pd.DataFrame(columns=["position","candidate","votes"])
    out = df.groupby(["position","candidate"]).size().reset_index(name="votes")
    return out.sort_values(["position","votes"], ascending=[True, False])

# --------------------------------------------------------------------------------------
# UI Tabs
# --------------------------------------------------------------------------------------
tab_vote, tab_results, tab_admin = st.tabs(["🗳️ Vote", "📊 Results", "🔑 Admin"])

# ------------------------ Vote Tab ------------------------
with tab_vote:
    st.subheader("ভোট দিন (টোকেন ব্যবহার করে)")
    token = st.text_input("আপনার টোকেন লিখুন", placeholder="BYWOB-2025-XXXXXX")

    if st.button("Proceed"):
        if not token:
            st.error("টোকেন দিন।")
            st.stop()

        if not is_voting_open():
            m = meta_get_all()
            st.error(
                "এখন ভোট গ্রহণ করা হচ্ছে না।\n\n"
                f"Status: {m.get('status','idle')}\n"
                f"Start (UTC): {m.get('start_utc','')}\n"
                f"End (UTC): {m.get('end_utc','')}"
            )
            st.stop()

        voters = load_voters_df()
        row = voters[voters["token"] == token.strip()]
        if row.empty:
            st.error("❌ টোকেন সঠিক নয়।")
            st.stop()
        if row["used_bool"].iloc[0]:
            st.error("⚠️ এই টোকেনটি ইতিমধ্যে ব্যবহার করা হয়েছে।")
            st.stop()

        cands = load_candidates_df()
        if cands.empty:
            st.warning("candidates শিট ফাঁকা। Admin ট্যাব থেকে প্রার্থী যোগ করুন।")
            st.stop()

        positions = cands["position"].unique().tolist()
        pos = st.selectbox("পদের নাম বাছাই করুন", positions, index=0)
        opts = cands[cands["position"] == pos]["candidate"].tolist()
        cand = st.selectbox("প্রার্থীর নাম বাছাই করুন", opts, index=0)

        if st.button("✅ Submit Vote"):
            append_vote(pos, cand)
            mark_token_used(voters, token)
            st.success("আপনার ভোট গ্রহণ করা হয়েছে। ধন্যবাদ!")

# ------------------------ Results Tab ------------------------
with tab_results:
    st.subheader("📊 Live Results")
    r = results_df()
    if r.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
    else:
        st.dataframe(r, width="stretch")

# ------------------------ Admin Tab ------------------------
with tab_admin:
    st.subheader("🛠️ Admin Tools")

    # Optional admin password
    admin_ok = True
    admin_pwd = st.secrets.get("ADMIN_PASSWORD")
    if admin_pwd:
        pwd = st.text_input("Admin password", type="password")
        admin_ok = (pwd == admin_pwd)
        if pwd and not admin_ok:
            st.error("Wrong password")

    if admin_ok:
        m = meta_get_all