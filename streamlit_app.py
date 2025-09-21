import streamlit as st
import pandas as pd
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import secrets, string

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
        df = pd.DataFrame(columns=["position","candidate"])
    return df

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

def save_vote_batch(selections, positions):
    now = datetime.utcnow().isoformat()
    rows = [[pos, selections[pos], now] for pos in positions]
    votes_ws.append_rows(rows, value_input_option="RAW")

def ballot_form(cands, positions):
    selections = {}
    with st.form("ballot_form"):
        st.info("প্রতিটি পদের জন্য একজন প্রার্থী নির্বাচন করুন, তারপর Submit ক্লিক করুন।")
        for pos in positions:
            subset = cands[cands["position"] == pos]["candidate"].tolist()
            options = [PLACEHOLDER] + subset
            choice = st.radio(f"**{pos}**", options, index=0, key=f"radio_{pos}")
            selections[pos] = None if choice == PLACEHOLDER else choice

        missing = [p for p in positions if selections[p] is None]
        can_submit = (len(missing) == 0)

        submitted = st.form_submit_button("✅ ভোট জমা দিন", disabled=not can_submit)
        return submitted, selections, missing

def tally(cands):
    votes = pd.DataFrame(votes_ws.get_all_records())
    if votes.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
        return
    st.subheader("📊 Live Tally")
    for pos in cands["position"].unique():
        st.markdown(f"**{pos}**")
        counts = votes[votes["position"] == pos]["candidate"].value_counts()
        st.table(counts.rename("votes"))

def generate_token(prefix="BYWOB-2025", n=8):
    alphabet = string.ascii_uppercase + string.digits
    return f"{prefix}-" + "".join(secrets.choice(alphabet) for _ in range(n))

def add_tokens(count, prefix="BYWOB-2025"):
    rows = []
    for _ in range(int(count)):
        t = generate_token(prefix=prefix, n=8)
        rows.append(["", "", t, False, ""])
    if rows:
        voters_ws.append_rows(rows, value_input_option="RAW")
        load_voters.clear()

# ---- Main UI ----
voters = load_voters()
cands  = load_candidates()
positions = cands["position"].unique().tolist()

tab_vote, tab_admin = st.tabs(["🔐 Vote", "🛠️ Admin"])

with tab_vote:
    token = st.text_input("আপনার ওয়ান-টাইম টোকেন লিখুন", type="password")
    if token:
        rec, err = validate_token(voters, token.strip())
        if err:
            st.error(err)
        else:
            st.success(f"স্বাগতম, {rec['name']} — সব পদের জন্য ভোট দিন।")
            submitted, selections, missing = ballot_form(cands, positions)
            if submitted:
                if len(missing) == 0:
                    mark_token_used(token.strip())
                    save_vote_batch(selections, positions)
                    st.success("আপনার ভোট সংরক্ষিত হয়েছে ✅")
                else:
                    st.error("প্রতিটি পদের জন্য একজন প্রার্থী নির্বাচন করুন।")

with tab_admin:
    st.caption("Admin ট্যাব (ADMIN_PASSWORD Secrets এ দিলে পাসওয়ার্ড লাগবে)")
    ok = True
    if ADMIN_PASSWORD:
        pw = st.text_input("Admin password", type="password")
        ok = (pw == ADMIN_PASSWORD)
        if pw and not ok:
            st.error("Wrong password")

    if ok:
        with st.expander("📋 Candidates"):
            st.dataframe(cands)
        with st.expander("🧑‍🤝‍🧑 Voters (tokens hidden)"):
            if not voters.empty:
                st.dataframe(voters.assign(token="••••••••"))
            else:
                st.info("কোনো ভোটার নেই।")
        with st.expander("🔑 Token Generator"):
            col1, col2 = st.columns(2)
            with col1:
                count = st.number_input("কতটি টোকেন?", min_value=1, max_value=2000, value=20, step=10)
            with col2:
                prefix = st.text_input("Prefix", value="BYWOB-2025")
            if st.button("➕ Generate & Append"):
                add_tokens(count, prefix=prefix)
                st.success(f"{int(count)}টি টোকেন voters শিটে যোগ হয়েছে।")
        with st.expander("📊 Live Tally"):
            tally(cands)
    else:
        st.warning("Please enter admin password.")
