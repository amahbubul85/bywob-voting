import streamlit as st
import pandas as pd
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import secrets, string, math

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + Google Sheets • Secret ballot with one-time tokens")

ADMIN_PASSWORD = st.secrets.get("ADMIN_PASSWORD", None)

# ---- Google Sheets setup ----
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
client = gspread.authorize(creds)
SHEET_ID = st.secrets["gcp_service_account"]["SHEET_ID"]
sheet = client.open_by_key(SHEET_ID)

voters_ws = sheet.worksheet("voters")
candidates_ws = sheet.worksheet("candidates")
votes_ws = sheet.worksheet("votes")

PLACEHOLDER = "— একজন প্রার্থী নির্বাচন করুন —"

# ---------------------------
# Loaders (clean + safe)
# ---------------------------
@st.cache_data
def load_voters():
    df = pd.DataFrame(voters_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["name","email","token","used","used_at"])
    return df

@st.cache_data
def load_candidates():
    df = pd.DataFrame(candidates_ws.get_all_records())
    if df.empty:
        return pd.DataFrame(columns=["position","candidate"])
    # স্পেস ট্রিম + ফাঁকা রো বাদ
    df["position"]  = df["position"].astype(str).str.strip()
    df["candidate"] = df["candidate"].astype(str).str.strip()
    df = df[(df["position"] != "") & (df["candidate"] != "")]
    return df

# ---------------------------
# Token helpers
# ---------------------------
def _boolish(x):
    return str(x).strip().lower() in ["true","1","yes"]

def validate_token(voters, token):
    row = voters.loc[voters["token"] == token]
    if row.empty:
        return None, "❌ টোকেন সঠিক নয়।"
    if _boolish(row.iloc[0]["used"]):
        return None, "⚠️ এই টোকেনটি ইতিমধ্যে ব্যবহার করা হয়েছে।"
    return row.iloc[0], None

def mark_token_used(token):
    voters = load_voters()
    idx = voters[voters["token"] == token].index
    if len(idx)==0:
        return
    row_index = idx[0] + 2
    used_col = voters.columns.get_loc("used") + 1
    used_at_col = voters.columns.get_loc("used_at") + 1
    voters_ws.update_cell(row_index, used_col, True)
    voters_ws.update_cell(row_index, used_at_col, datetime.utcnow().isoformat())
    load_voters.clear()

# ---------------------------
# Voting helpers
# ---------------------------
def save_vote_batch(selections, positions):
    now = datetime.utcnow().isoformat()
    rows = [[pos, selections[pos], now] for pos in positions]
    votes_ws.append_rows(rows, value_input_option="RAW")

def ballot_form(cands, positions):
    """সব পদের রেডিও এক পেজে; সব সিলেক্ট না হলে Submit নিষ্ক্রিয়।"""
    selections = {}
    with st.form("ballot_form"):
        st.info("প্রতিটি পদের জন্য একজন প্রার্থী নির্বাচন করুন, তারপর নিচের Submit বাটনে ক্লিক করুন।")
        for pos in positions:
            subset = cands[cands["position"] == pos]["candidate"].tolist()
            options = [PLACEHOLDER] + subset
            choice = st.radio(f"**{pos}**", options, index=0, key=f"radio_{pos}")
            selections[pos] = None if choice == PLACEHOLDER else choice

        missing = [p for p in positions if selections[p] is None]
        can_submit = (len(missing) == 0)

        # নতুন: সব সিলেক্ট না হলে বাটন disabled
        submitted = st.form_submit_button("✅ ভোট জমা দিন", disabled=not can_submit)

        return submitted, selections, missing

def tally(cands):
    votes = pd.DataFrame(votes_ws.get_all_records())
    if votes.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
        return
