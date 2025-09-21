# streamlit_app.py
# BYWOB Voting (Streamlit + Google Sheets)
# - Fixes "Invalid or already used token" by normalizing 'used'
# - Uses triple-quoted private_key in Secrets
# - Token generator & live results
# - Minimal, robust error handling

import streamlit as st
import pandas as pd
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️")

st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + Google Sheets • Secret ballot with one-time tokens")

# -----------------------------
# Google Sheets connection
# -----------------------------
def _require_secrets():
    missing = []
    for k in ["gcp_service_account"]:
        if k not in st.secrets:
            missing.append(k)
    if missing:
        st.error(f"Secrets missing: {', '.join(missing)}. Add them in Settings → Secrets.")
        st.stop()

_require_secrets()

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
client = gspread.authorize(creds)

SHEET_ID = st.secrets["gcp_service_account"]["SHEET_ID"]
sheet = client.open_by_key(SHEET_ID)

# Worksheet handlers
def _ws(name: str):
    try:
        return sheet.worksheet(name)
    except Exception as e:
        st.error(f"Worksheet '{name}' not found. Please create it with correct headers.")
        st.stop()

voters_ws = _ws("voters")
candidates_ws = _ws("candidates")
votes_ws = _ws("votes")

# -----------------------------
# Data loaders (cached)
# -----------------------------
@st.cache_data(show_spinner=False)
def load_voters_df():
    df = pd.DataFrame(voters_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["name", "email", "token", "used", "used_at"])
    # normalize columns
    cols = {c.strip().lower(): c for c in df.columns}
    # ensure expected columns exist
    for col in ["name","email","token","used","used_at"]:
        if col not in [c.strip().lower() for c in df.columns]:
            df[col] = "" if col in ["name","email","token","used_at"] else False
    # rebuild with correct order
    df = df[[cols.get("name","name"),
             cols.get("email","email"),
             cols.get("token","token"),
             cols.get("used","used"),
             cols.get("used_at","used_at")]]
    df.columns = ["name","email","token","used","used_at"]
    # strip tokens & make used_bool
    df["token"] = df["token"].astype(str).str.strip()
    df["used_bool"] = df["used"].astype(str).str.strip().str.lower().isin(["true","1","yes"])
    return df

@st.cache_data(show_spinner=False)
def load_candidates_df():
    df = pd.DataFrame(candidates_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["position", "candidate"])
    # normalize
    if "position" not in df.columns or "candidate" not in df.columns:
        # try lowercase rescue
        cols = {c.strip().lower(): c for c in df.columns}
        df = df[[cols.get("position","position"), cols.get("candidate","candidate")]]
        df.columns = ["position","candidate"]
    df["position"] = df["position"].astype(str).str.strip()
    df["candidate"] = df["candidate"].astype(str).str.strip()
    # drop empty positions/candidates rows
    df = df[(df["position"]!="") & (df["candidate"]!="")]
    return df

@st.cache_data(show_spinner=False)
def load_votes_df():
    df = pd.DataFrame(votes_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["position","candidate","timestamp"])
    return df

def clear_caches():
    load_voters_df.clear()
    load_candidates_df.clear()
    load_votes_df.clear()

# -----------------------------
# Helpers
# -----------------------------
def mark_token_used(df_voters: pd.DataFrame, token: str):
    token_clean = str(token).strip()
    # Find index in the *current* voters DF
    match = df_voters[df_voters["token"] == token_clean]
    if match.empty:
        return
    row_index = match.index[0] + 2  # +2 because header row is 1-based and DataFrame is 0-based
    # used column is 4th, used_at is 5th (1-based)
    voters_ws.update_cell(row_index, 4, "TRUE")
    voters_ws.update_cell(row_index, 5, datetime.utcnow().isoformat())
    load_voters_df.clear()

def append_votes(rows):
    # rows: List[List[position, candidate, timestamp]]
    if rows:
        votes_ws.append_rows(rows, value_input_option="RAW")
        load_votes_df.clear()

def generate_tokens(count: int, prefix: str):
    import secrets, string
    alpha = string.ascii_uppercase + string.digits
    rows = []
    for _ in range(count):
        t = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        rows.append(["", "", t, "FALSE", ""])  # name, email, token, used, used_at
    if rows:
        voters_ws.append_rows(rows, value_input_option="RAW")
        load_voters_df.clear()

# -----------------------------
# UI
# -----------------------------
tab_vote, tab_results, tab_admin = st.tabs(["🗳️ Vote", "📊 Results", "🔑 Admin"])

with tab_vote:
    st.subheader("ভোট দিন (টোকেন ব্যবহার করে)")
    token_input = st.text_input("আপনার টোকেন লিখুন", placeholder="BYWOB-2025-XXXXXX")
    if st.button("Proceed") and token_input:
        voters = load_voters_df()
        token_clean = token_input.strip()
        row = voters[voters["token"] == token_clean]

        if row.empty:
            st.error("❌ টোকেন সঠিক নয়।")
            st.stop()

        if row["used_bool"].iloc[0]:
            st.error("⚠️ এই টোকেনটি ইতিমধ্যে ব্যবহার করা হয়েছে।")
            st.stop()

        candidates = load_candidates_df()
        if candidates.empty:
            st.warning("কোনো প্রার্থী সেট করা নেই। Admin ট্যাব থেকে candidates শিট ভরুন।")
            st.stop()

        positions = candidates["position"].dropna().unique().tolist()
        position = st.selectbox("পদের নাম বাছাই করুন", positions, index=0)
        subset = candidates[candidates["position"] == position]
        cand_opts = subset["candidate"].tolist()
        if not cand_opts:
            st.warning("এই পদের জন্য কোনো প্রার্থী নেই।")
            st.stop()
        candidate = st.selectbox("প্রার্থীর নাম বাছাই করুন", cand_opts, index=0)

        if st.button("✅ Submit Vote"):
            now = datetime.utcnow().isoformat()
            append_votes([[position, candidate, now]])
            mark_token_used(voters, token_clean)
            st.success("আপনার ভোট গ্রহণ করা হয়েছে। ধন্যবাদ!")

with tab_results:
    st.subheader("📊 Live Results")
    votes = load_votes_df()
    if votes.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
    else:
        # group & show
        grp = (
            votes.groupby(["position", "candidate"])
            .size()
            .reset_index(name="votes")
            .sort_values(["position", "votes"], ascending=[True, False])
        )
        st.dataframe(grp, use_container_width=True)

with tab_admin:
    st.subheader("🛠️ Admin Tools")
    admin_ok = True
    admin_pwd = st.secrets.get("ADMIN_PASSWORD")
    if admin_pwd:
        pwd = st.text_input("Admin password", type="password")
        admin_ok = (pwd == admin_pwd)
        if pwd and not admin_ok:
            st.error("Wrong password")

    if admin_ok:
        st.markdown("### 🔑 Token Generator")
        col1, col2 = st.columns(2)
        with col1:
            count = st.number_input("কতটি টোকেন?", min_value=1, max_value=2000, value=20, step=10)
        with col2:
            prefix = st.text_input("Prefix", value="BYWOB-2025")

        if st.button("➕ Generate & Append"):
            try:
                generate_tokens(int(count), prefix)
                st.success(f"{int(count)}টি টোকেন voters শিটে যোগ হয়েছে।")
            except Exception as e:
                st.error(f"টোকেন তৈরি করা যায়নি: {e}")

        st.markdown("### 👥 Voters (tokens hidden)")
        voters_df = load_voters_df().copy()
        if voters_df.empty:
            st.info("কোনো ভোটার নেই।")
        else:
            safe = voters_df.copy()
            safe["token"] = "••••••••"
            st.dataframe(safe.drop(columns=["used_bool"]), use_container_width=True)

        st.markdown("### 📋 Candidates")
        cands_df = load_candidates_df()
        if cands_df.empty:
            st.info("candidates শিট ফাঁকা। position, candidate কলামসহ ডেটা দিন।")
        else:
            st.dataframe(cands_df, use_container_width=True)

        st.markdown("### 📈 Tally (by position)")
        votes_df = load_votes_df()
        if votes_df.empty:
            st.info("এখনও কোনো ভোট পড়েনি।")
        else:
            for pos in cands_df["position"].unique():
                pos_grp = (
                    votes_df[votes_df["position"] == pos]
                    .groupby("candidate")
                    .size()
                    .reset_index(name="votes")
                    .sort_values("votes", ascending=False)
                )
                if not pos_grp.empty:
                    st.markdown(f"**{pos}**")
                    st.table(pos_grp.set_index("candidate"))

    else:
        st.warning("Please enter admin password to continue.")
