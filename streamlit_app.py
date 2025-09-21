# streamlit_app.py
# BYWOB Online Voting — Streamlit + Google Sheets
# Features:
# - One-time token voting
# - Election window (start/end in UTC): idle | ongoing | ended | published
# - Block votes outside window, publish/declare results
# - Token generator (no hard max)
# - Live tally, CSV export
# - Archive & clear votes for next election
# - Robust 'used' handling (string/boolean)

import streamlit as st
import pandas as pd
from datetime import datetime, timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="centered")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + Google Sheets • Secret ballot with one-time tokens")

# --------------------------------------------------------------------------------------
# Secrets & Google Sheets connection
# --------------------------------------------------------------------------------------
def _require_secrets():
    if "gcp_service_account" not in st.secrets:
        st.error(
            "Secrets missing: gcp_service_account. "
            "App → Settings → Secrets এ আপনার service account JSON এবং SHEET_ID বসান।"
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
        st.error(f"Worksheet '{name}' পাওয়া যায়নি। দয়া করে শিটে এই ট্যাবটি তৈরি করুন।")
        st.stop()

voters_ws = _ws("voters")         # headers: name | email | token | used | used_at
candidates_ws = _ws("candidates") # headers: position | candidate
votes_ws = _ws("votes")           # headers: position | candidate | timestamp

# --------------------------------------------------------------------------------------
# Election meta helpers (persisted in 'election_meta')
# --------------------------------------------------------------------------------------
ELECTION_META_SHEET = "election_meta"  # columns: key | value

def ensure_meta_sheet():
    try:
        ws = sheet.worksheet(ELECTION_META_SHEET)
    except Exception:
        ws = sheet.add_worksheet(title=ELECTION_META_SHEET, rows=20, cols=3)
        ws.update("A1:B1", [["key", "value"]])
        default = [
            ["name", ""],
            ["status", "idle"],      # idle | ongoing | ended | published
            ["start_utc", ""],       # ISO8601 (UTC)
            ["end_utc", ""],         # ISO8601 (UTC)
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
    for i, r in enumerate(recs, start=2):  # +1 header, +1 one-based
        if r.get("key") == key:
            ws.update_cell(i, 2, value)
            return
    ws.append_row([key, value], value_input_option="RAW")

def now_utc():
    return datetime.now(timezone.utc)

def to_utc_iso(dt_obj: datetime) -> str:
    """Coerce any datetime (naive/aware) to UTC ISO string."""
    if dt_obj.tzinfo is None:
        # treat input as UTC if naive
        return dt_obj.replace(tzinfo=timezone.utc).isoformat()
    return dt_obj.astimezone(timezone.utc).isoformat()

def is_voting_open():
    meta, _ = read_meta()
    if meta.get("status", "idle") != "ongoing":
        return False
    start = meta.get("start_utc", "")
    end = meta.get("end_utc", "")
    try:
        start_dt = datetime.fromisoformat(start) if start else None
        end_dt = datetime.fromisoformat(end) if end else None
    except Exception:
        return False
    now = now_utc()
    if start_dt and now < start_dt.astimezone(timezone.utc):
        return False
    if end_dt and now > end_dt.astimezone(timezone.utc):
        set_meta("status", "ended")  # auto-end if passed
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
    cols_lower = {c.strip().lower(): c for c in df.columns}
    # ensure expected columns
    for col in ["name", "email", "token", "used", "used_at"]:
        if col not in cols_lower:
            df[col] = "" if col in ["name", "email", "token", "used_at"] else False
    df = df[[cols_lower.get("name", "name"),
             cols_lower.get("email", "email"),
             cols_lower.get("token", "token"),
             cols_lower.get("used", "used"),
             cols_lower.get("used_at", "used_at")]]
    df.columns = ["name", "email", "token", "used", "used_at"]
    df["token"] = df["token"].astype(str).str.strip()
    df["used_bool"] = df["used"].astype(str).str.strip().str.lower().isin(["true", "1", "yes"])
    return df

@st.cache_data(show_spinner=False)
def load_candidates_df():
    df = pd.DataFrame(candidates_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["position", "candidate"])
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
# Sheet ops
# --------------------------------------------------------------------------------------
def mark_token_used(df_voters: pd.DataFrame, token: str):
    token_clean = str(token).strip()
    m = df_voters[df_voters["token"] == token_clean]
    if m.empty:
        return
    row_index = m.index[0] + 2  # +2 => header row + 1-based
    voters_ws.update_cell(row_index, 4, "TRUE")                          # used
    voters_ws.update_cell(row_index, 5, now_utc().isoformat())           # used_at
    load_voters_df.clear()

def append_votes(rows):
    if rows:
        votes_ws.append_rows(rows, value_input_option="RAW")
        load_votes_df.clear()

def generate_tokens(count: int, prefix: str):
    import secrets, string
    alpha = string.ascii_uppercase + string.digits
    rows = []
    for _ in range(count):
        t = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        rows.append(["", "", t, "FALSE", ""])
    if rows:
        voters_ws.append_rows(rows, value_input_option="RAW")
        load_voters_df.clear()

def archive_and_clear_votes(election_name: str = None):
    votes = votes_ws.get_all_records()
    if not votes:
        return "no_votes"
    ts = now_utc().strftime("%Y%m%dT%H%M%SZ")
    safe_name = (election_name or "election").replace(" ", "_")[:20]
    archive_title = f"votes_archive_{safe_name}_{ts}"
    new_ws = sheet.add_worksheet(title=archive_title, rows=len(votes) + 5, cols=5)
    new_ws.update("A1:C1", [["position", "candidate", "timestamp"]])
    new_ws.append_rows(
        [[v["position"], v["candidate"], v["timestamp"]] for v in votes],
        value_input_option="RAW",
    )
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

    # Optional password protection
    admin_ok = True
    admin_pwd = st.secrets.get("ADMIN_PASSWORD")
    if admin_pwd:
        pwd = st.text_input("Admin password", type="password")
        admin_ok = (pwd == admin_pwd)
        if pwd and not admin_ok:
            st.error("Wrong password")

    if admin_ok:
        st.markdown("### 🗓️ Election control")
        meta, _ = read_meta()
        st.markdown(f"- **Current election name:** `{meta.get('name','(none)')}`")
        st.markdown(f"- **Status:** `{meta.get('status','idle')}`")
        st.markdown(f"- **Start (UTC):** `{meta.get('start_utc','')}`")
        st.markdown(f"- **End (UTC):** `{meta.get('end_utc','')}`")
        st.markdown(f"- **Published:** `{meta.get('published','FALSE')}`")

        st.divider()
        st.markdown("#### Create / Schedule new election")

        # ✅ Use UTC-aware defaults for datetime_input
        default_start = datetime.now(timezone.utc)
        default_end = datetime.now(timezone.utc)

        c1, c2 = st.columns(2)
        start_dt = c1.datetime_input("Start (UTC)", value=default_start)
        end_dt   = c2.datetime_input("End (UTC)", value=default_end)

        ename = st.text_input("Election name", value=meta.get("name", ""))

        if st.button("Set & Schedule"):
            set_meta("name", ename)
            set_meta("start_utc", to_utc_iso(start_dt))
            set_meta("end_utc", to_utc_iso(end_dt))
            set_meta("status", "idle")
            set_meta("published", "FALSE")
            st.success("Election scheduled. সময় হলে 'Start Election Now' চাপুন বা অটো-উইন্ডোতে শুরু হবে।")

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
            st.success("Results published. Export/Archive করতে পারেন।")

        st.divider()
        st.markdown("### 🔑 Token Generator")
        g1, g2 = st.columns(2)
        count = g1.number_input("কতটি টোকেন?", min_value=1, value=20, step=10)  # no max
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
