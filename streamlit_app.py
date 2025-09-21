# streamlit_app.py
# BYWOB Online Voting — Streamlit + Google Sheets
# Features:
# - One-time token voting (secret ballot)
# - Election window (start/end in UTC) with status: idle | ongoing | ended | published
# - Block votes outside window, publish/declare results
# - Token generator (unlimited)
# - Live tally, CSV export
# - Archive & clear votes to prepare next election
# - Robust handling of 'used' column being string/boolean

import streamlit as st
import pandas as pd
from datetime import datetime, timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import io

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="centered")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + Google Sheets • Secret ballot with one-time tokens")

# --------------------------------------------------------------------------------------
# Secrets & Google Sheets connection
# --------------------------------------------------------------------------------------
def _require_secrets():
    missing = []
    if "gcp_service_account" not in st.secrets:
        missing.append("gcp_service_account")
    if missing:
        st.error(
            "Secrets missing: "
            + ", ".join(missing)
            + ". Add them in App → Settings → Secrets. "
            "See docs for 'Streamlit Secrets'."
        )
        st.stop()

_require_secrets()

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
client = gspread.authorize(creds)

SHEET_ID = st.secrets["gcp_service_account"]["SHEET_ID"]
sheet = client.open_by_key(SHEET_ID)

def _ws(name: str):
    try:
        return sheet.worksheet(name)
    except Exception:
        st.error(f"Worksheet '{name}' not found. Please create it with correct headers.")
        st.stop()

voters_ws = _ws("voters")         # headers: name | email | token | used | used_at
candidates_ws = _ws("candidates") # headers: position | candidate
votes_ws = _ws("votes")           # headers: position | candidate | timestamp

# --------------------------------------------------------------------------------------
# Election meta helpers (persisted in a sheet called 'election_meta')
# --------------------------------------------------------------------------------------
ELECTION_META_SHEET = "election_meta"  # key | value

def ensure_meta_sheet():
    try:
        ws = sheet.worksheet(ELECTION_META_SHEET)
    except Exception:
        ws = sheet.add_worksheet(title=ELECTION_META_SHEET, rows=20, cols=3)
        ws.update("A1:B1", [["key", "value"]])
        default = [
            ["name", ""],
            ["status", "idle"],      # idle | ongoing | ended | published
            ["start_utc", ""],       # ISO8601
            ["end_utc", ""],         # ISO8601
            ["published", "FALSE"],
            ["election_id", ""],
        ]
        ws.append_rows(default, value_input_option="RAW")
    return sheet.worksheet(ELECTION_META_SHEET)

def read_meta():
    ws = ensure_meta_sheet()
    recs = ws.get_all_records()
    meta = {r["key"]: r["value"] for r in recs if r.get("key")}
    return meta, ws

def set_meta(key: str, value: str):
    ws = ensure_meta_sheet()
    recs = ws.get_all_records()
    for i, r in enumerate(recs, start=2):  # +1 for header, +1 for 1-based index
        if r.get("key") == key:
            ws.update_cell(i, 2, value)
            return
    ws.append_row([key, value], value_input_option="RAW")

def now_utc():
    return datetime.utcnow().replace(tzinfo=timezone.utc)

def is_voting_open():
    meta, _ = read_meta()
    status = meta.get("status", "idle")
    if status != "ongoing":
        return False
    start = meta.get("start_utc", "")
    end = meta.get("end_utc", "")
    try:
        start_dt = datetime.fromisoformat(start) if start else None
        end_dt = datetime.fromisoformat(end) if end else None
    except Exception:
        return False
    now = now_utc()
    if start_dt and now < start_dt.replace(tzinfo=timezone.utc):
        return False
    if end_dt and now > end_dt.replace(tzinfo=timezone.utc):
        # auto-end if passed
        set_meta("status", "ended")
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
    # normalize headers -> ensure expected columns exist
    cols_lower = {c.strip().lower(): c for c in df.columns}
    for needed in ["name", "email", "token", "used", "used_at"]:
        if needed not in cols_lower:
            df[needed] = "" if needed in ["name", "email", "token", "used_at"] else False
    # select in correct order
    df = df[[cols_lower.get("name", "name"),
             cols_lower.get("email", "email"),
             cols_lower.get("token", "token"),
             cols_lower.get("used", "used"),
             cols_lower.get("used_at", "used_at")]]
    df.columns = ["name", "email", "token", "used", "used_at"]
    # clean & normalize
    df["token"] = df["token"].astype(str).str.strip()
    df["used_bool"] = df["used"].astype(str).str.strip().str.lower().isin(["true", "1", "yes"])
    return df

@st.cache_data(show_spinner=False)
def load_candidates_df():
    df = pd.DataFrame(candidates_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["position", "candidate"])
    # try normalize
    if "position" not in df.columns or "candidate" not in df.columns:
        cols_lower = {c.strip().lower(): c for c in df.columns}
        df = df[[cols_lower.get("position", "position"), cols_lower.get("candidate", "candidate")]]
        df.columns = ["position", "candidate"]
    df["position"] = df["position"].astype(str).str.strip()
    df["candidate"] = df["candidate"].astype(str).str.strip()
    df = df[(df["position"] != "") & (df["candidate"] != "")]
    return df

@st.cache_data(show_spinner=False)
def load_votes_df():
    df = pd.DataFrame(votes_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["position", "candidate", "timestamp"])
    return df

def clear_caches():
    load_voters_df.clear()
    load_candidates_df.clear()
    load_votes_df.clear()

# --------------------------------------------------------------------------------------
# Sheet operations
# --------------------------------------------------------------------------------------
def mark_token_used(df_voters: pd.DataFrame, token: str):
    token_clean = str(token).strip()
    m = df_voters[df_voters["token"] == token_clean]
    if m.empty:
        return
    row_index = m.index[0] + 2  # +2 => header is row 1, DataFrame is 0-based
    voters_ws.update_cell(row_index, 4, "TRUE")  # 'used'
    voters_ws.update_cell(row_index, 5, now_utc().isoformat())  # 'used_at'
    load_voters_df.clear()

def append_votes(rows):
    # rows: [[position, candidate, timestamp], ...]
    if rows:
        votes_ws.append_rows(rows, value_input_option="RAW")
        load_votes_df.clear()

def generate_tokens(count: int, prefix: str):
    import secrets, string
    alpha = string.ascii_uppercase + string.digits
    out = []
    for _ in range(count):
        t = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        out.append(["", "", t, "FALSE", ""])
    if out:
        voters_ws.append_rows(out, value_input_option="RAW")
        load_voters_df.clear()

def archive_and_clear_votes(election_name: str = None):
    votes = votes_ws.get_all_records()
    if not votes:
        return "no_votes"
    ts = now_utc().strftime("%Y%m%dT%H%M%SZ")
    safe_name = (election_name or "election").replace(" ", "_")[:20]
    archive_title = f"votes_archive_{safe_name}_{ts}"
    new_ws = sheet.add_worksheet(title=archive_title, rows=len(votes) + 5, cols=5)
    # header
    new_ws.update("A1:C1", [["position", "candidate", "timestamp"]])
    new_ws.append_rows(
        [[v["position"], v["candidate"], v["timestamp"]] for v in votes],
        value_input_option="RAW",
    )
    # clear current votes sheet (keep worksheet & header)
    votes_ws.clear()
    votes_ws.append_row(["position", "candidate", "timestamp"], value_input_option="RAW")
    load_votes_df.clear()
    return archive_title

def get_results_dataframe():
    votes = load_votes_df()
    if votes.empty:
        return pd.DataFrame(columns=["position", "candidate", "votes"])
    grouped = votes.groupby(["position", "candidate"]).size().reset_index(name="votes")
    return grouped.sort_values(["position", "votes"], ascending=[True, False])

# --------------------------------------------------------------------------------------
# UI Tabs
# --------------------------------------------------------------------------------------
tab_vote, tab_results, tab_admin = st.tabs(["🗳️ Vote", "📊 Results", "🔑 Admin"])

# ------------------------ Vote Tab ------------------------
with tab_vote:
    st.subheader("ভোট দিন (টোকেন ব্যবহার করে)")
    token_input = st.text_input("আপনার টোকেন লিখুন", placeholder="BYWOB-2025-XXXXXX")

    if st.button("Proceed"):
        if not token_input:
            st.error("টোকেন দিন।")
            st.stop()

        # election window check
        if not is_voting_open():
            meta, _ = read_meta()
            st.error(
                "এখন ভোট গ্রহণ করা হচ্ছে না।\n\n"
                f"Status: {meta.get('status','idle')}\n"
                f"Start (UTC): {meta.get('start_utc','')}\n"
                f"End (UTC): {meta.get('end_utc','')}"
            )
            st.stop()

        voters = load_voters_df()
        token_clean = token_input.strip()
        row = voters[voters["token"] == token_clean]

        if row.empty:
            st.error("❌ টোকেন সঠিক নয়।")
            st.stop()

        if row["used_bool"].iloc[0]:
            st.error("⚠️ এই টোকেনটি ইতিমধ্যে ব্যবহার করা হয়েছে।")
            st.stop()

        # load candidates
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
            ts = now_utc().isoformat()
            append_votes([[position, candidate, ts]])
            mark_token_used(voters, token_clean)
            st.success("আপনার ভোট গ্রহণ করা হয়েছে। ধন্যবাদ!")

# ------------------------ Results Tab ------------------------
with tab_results:
    st.subheader("📊 Live Results")
    df = get_results_dataframe()
    if df.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
    else:
        st.dataframe(df, use_container_width=True)

# ------------------------ Admin Tab ------------------------
with tab_admin:
    st.subheader("🛠️ Admin Tools")

    # Optional password
    admin_ok = True
    admin_pwd = st.secrets.get("ADMIN_PASSWORD")
    if admin_pwd:
        pwd = st.text_input("Admin password", type="password")
        admin_ok = (pwd == admin_pwd)
        if pwd and not admin_ok:
            st.error("Wrong password")

    if admin_ok:
        # Election control
        st.markdown("### 🗓️ Election control")
        meta, _ = read_meta()
        st.markdown(f"- **Current election name:** `{meta.get('name','(none)')}`")
        st.markdown(f"- **Status:** `{meta.get('status','idle')}`")
        st.markdown(f"- **Start (UTC):** `{meta.get('start_utc','')}`")
        st.markdown(f"- **End (UTC):** `{meta.get('end_utc','')}`")
        st.markdown(f"- **Published:** `{meta.get('published','FALSE')}`")

        st.divider()
        st.markdown("#### Create / Schedule new election")
        ename = st.text_input("Election name", value=meta.get("name", ""))
        c1, c2 = st.columns(2)
        start_dt = c1.datetime_input("Start (UTC)", value=datetime.utcnow())
        end_dt = c2.datetime_input("End (UTC)", value=datetime.utcnow())

        if st.button("Set & Schedule"):
            set_meta("name", ename)
            set_meta("start_utc", start_dt.replace(tzinfo=timezone.utc).isoformat())
            set_meta("end_utc", end_dt.replace(tzinfo=timezone.utc).isoformat())
            set_meta("status", "idle")
            set_meta("published", "FALSE")
            st.success("Election scheduled. Press 'Start Election Now' at the right time (or wait).")

        c3, c4, c5 = st.columns(3)
        if c3.button("Start Election Now"):
            set_meta("status", "ongoing")
            st.success("Election started (status = ongoing).")

        if c4.button("End Election Now"):
            set_meta("status", "ended")
            st.success("Election ended (status = ended).")

        if c5.button("Publish Results (declare)"):
            set_meta("published", "TRUE")
            set_meta("status", "ended")
            st.success("Results published. You can now export or archive votes.")

        st.divider()
        # Token generator
        st.markdown("### 🔑 Token Generator")
        g1, g2 = st.columns(2)
        count = g1.number_input("কতটি টোকেন?", min_value=1, value=20, step=10)  # unlimited (no max)
        prefix = g2.text_input("Prefix", value="BYWOB-2025")
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

        st.divider()
        st.markdown("### ⬇️ Export results")
        results_df = get_results_dataframe()
        if results_df.empty:
            st.info("No votes yet.")
        else:
            csv_bytes = results_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download results CSV",
                data=csv_bytes,
                file_name=f"results_{meta.get('name','election')}.csv",
                mime="text/csv",
            )

        st.markdown("### 🗄️ Archive & Clear")
        if st.button("Archive votes and clear (prepare new)"):
            name_for_archive = meta.get("name", "election")
            res = archive_and_clear_votes(name_for_archive)
            if res == "no_votes":
                st.info("No votes to archive.")
            else:
                st.success(f"Votes archived to sheet: {res}")

    else:
        st.warning("Please enter admin password to continue.")
