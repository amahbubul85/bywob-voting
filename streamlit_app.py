# streamlit_app.py
# BYWOB Online Voting — Streamlit + Google Sheets
# - Auto-creates required worksheets with headers (meta, voters, candidates, votes)
# - One-time token voting
# - Election window (start/end in CET): idle | ongoing | ended | published
# - Block votes outside window, publish/declare results
# - Token generator (no hard max)
# - Live tally, CSV export
# - Archive & clear votes

import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone, timedelta
import time
from functools import wraps

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="centered")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + Google Sheets • Secret ballot with one-time tokens")

# --------------------------------------------------------------------------------------
# CET Timezone Setup
# --------------------------------------------------------------------------------------
CET = timezone(timedelta(hours=1))  # CET is UTC+1

def now_cet():
    return datetime.now(CET)

def to_cet(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=CET)
    return dt.astimezone(CET)

# --------------------------------------------------------------------------------------
# API Rate Limiting Decorator
# --------------------------------------------------------------------------------------
def rate_limited(max_per_minute):
    """API কল রেট লিমিটার ডেকোরেটর"""
    min_interval = 60.0 / max_per minute
    
    def decorator(func):
        last_called = [0.0]
        
        @wraps(func)
        def rate_limited_function(*args, **kwargs):
            elapsed = time.time() - last_called[0]
            left_to_wait = min_interval - elapsed
            if left_to_wait > 0:
                time.sleep(left_to_wait)
            ret = func(*args, **kwargs)
            last_called[0] = time.time()
            return ret
        return rate_limited_function
    return decorator

# প্রতি মিনিটে সর্বাধিক 60টি রিকোয়েস্ট (Google Sheets API সীমা)
@rate_limited(60)
def rate_limited_api_call(api_function, *args, **kwargs):
    """রেট লিমিটেড API কল"""
    return api_function(*args, **kwargs)

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
# Safe Sheet Operations with Retry Logic
# --------------------------------------------------------------------------------------
def safe_sheet_operation(operation, max_retries=3, delay_seconds=2):
    """API কলগুলিতে রিট্রাই মেকানিজম যোগ করুন"""
    for attempt in range(max_retries):
        try:
            return rate_limited_api_call(operation)
        except Exception as e:
            if ("quota" in str(e).lower() or "429" in str(e)) and attempt < max_retries - 1:
                time.sleep(delay_seconds * (attempt + 1))
                continue
            raise e

# --------------------------------------------------------------------------------------
# Worksheet ensure/create helpers
# --------------------------------------------------------------------------------------
def ensure_ws(title: str, headers: list[str], rows: int = 100, cols: int = 10):
    """Return worksheet; create if missing and set headers."""
    try:
        ws = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        def create_ws():
            return sheet.add_worksheet(title=title, rows=rows, cols=cols)
        ws = safe_sheet_operation(create_ws)
        # write headers in row 1
        def update_headers():
            rng = f"A1:{chr(64+len(headers))}1"
            ws.update(values=[headers], range_name=rng)
        safe_sheet_operation(update_headers)
    return sheet.worksheet(title)

# Create or fetch all required sheets
meta_ws        = ensure_ws("meta", ["key", "value"], rows=20, cols=2)
voters_ws      = ensure_ws("voters", ["name", "email", "token", "used", "used_at"], rows=2000, cols=5)
candidates_ws  = ensure_ws("candidates", ["position", "candidate"], rows=500, cols=2)
votes_ws       = ensure_ws("votes", ["position", "candidate", "timestamp"], rows=5000, cols=3)

# --------------------------------------------------------------------------------------
# Meta helpers (status & schedule)
# --------------------------------------------------------------------------------------
def meta_get_all() -> dict:
    def get_records():
        return meta_ws.get_all_records()
    recs = safe_sheet_operation(get_records)
    return {r.get("key"): r.get("value") for r in recs if r.get("key")}

def meta_set(key: str, value: str):
    def get_records():
        return meta_ws.get_all_records()
    recs = safe_sheet_operation(get_records)
    # find existing key row (1-based with header)
    for i, r in enumerate(recs, start=2):
        if r.get("key") == key:
            def update_cell():
                meta_ws.update_cell(i, 2, value)
            safe_sheet_operation(update_cell)
            return
    # append new
    def append_row():
        meta_ws.append_row([key, value], value_input_option="RAW")
    safe_sheet_operation(append_row)

# Set defaults if first run
_meta = meta_get_all()
if "status" not in _meta:     meta_set("status", "idle")         # idle | ongoing | ended | published
if "name" not in _meta:       meta_set("name", "")
if "start_cet" not in _meta:  meta_set("start_cet", "")
if "end_cet" not in _meta:    meta_set("end_cet", "")
if "published" not in _meta:  meta_set("published", "FALSE")

def is_voting_open() -> bool:
    m = meta_get_all()
    if m.get("status", "idle") != "ongoing":
        return False
    try:
        start = m.get("start_cet", "")
        end   = m.get("end_cet", "")
        start_dt = datetime.fromisoformat(start) if start else None
        end_dt   = datetime.fromisoformat(end) if end else None
    except Exception:
        return False
    now = now_cet()
    if start_dt and now < to_cet(start_dt):
        return False
    if end_dt and now > to_cet(end_dt):
        meta_set("status", "ended")  # auto-close
        return False
    return True

# --------------------------------------------------------------------------------------
# Cached loaders - আরও দীর্ঘ সময়ের জন্য ক্যাশিং
# --------------------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=300)  # 5 মিনিট ক্যাশ
def load_voters_df():
    def get_records():
        return voters_ws.get_all_records()
    df = pd.DataFrame(safe_sheet_operation(get_records))
    if df.empty:
        df = pd.DataFrame(columns=["name", "email", "token", "used", "used_at"])
    # normalize
    for col in ["name", "email", "token", "used", "used_at"]:
        if col not in df.columns:
            df[col] = ""
    df["token"] = df["token"].astype(str).str.strip()
    df["used_bool"] = df["used"].astype(str).str.strip().str.lower().isin(["true", "1", "yes"])
    return df[["name", "email", "token", "used", "used_at", "used_bool"]]

@st.cache_data(show_spinner=False, ttl=300)  # 5 মিনিট ক্যাশ
def load_candidates_df():
    def get_records():
        return candidates_ws.get_all_records()
    df = pd.DataFrame(safe_sheet_operation(get_records))
    if df.empty:
        df = pd.DataFrame(columns=["position", "candidate"])
    for col in ["position", "candidate"]:
        if col not in df.columns: df[col] = ""
    df["position"] = df["position"].astype(str).str.strip()
    df["candidate"] = df["candidate"].astype(str).str.strip()
    df = df[(df["position"] != "") & (df["candidate"] != "")]
    return df[["position", "candidate"]]

@st.cache_data(show_spinner=False, ttl=300)  # 5 মিনিট ক্যাশ
def load_votes_df():
    def get_records():
        return votes_ws.get_all_records()
    df = pd.DataFrame(safe_sheet_operation(get_records))
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
    
    def update_operation():
        voters_ws.update_cell(row_idx, 4, "TRUE")                    # used
        voters_ws.update_cell(row_idx, 5, now_cet().isoformat())     # used_at
    
    safe_sheet_operation(update_operation)
    load_voters_df.clear()

def append_vote(position: str, candidate: str):
    def append_operation():
        votes_ws.append_row([position, candidate, now_cet().isoformat()], value_input_option="RAW")
    
    safe_sheet_operation(append_operation)
    load_votes_df.clear()

def generate_tokens(n: int, prefix: str):
    import secrets, string
    alpha = string.ascii_uppercase + string.digits
    rows = []
    for _ in range(n):
        tok = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        rows.append(["", "", tok, "FALSE", ""])
    
    if rows:
        def append_operation():
            voters_ws.append_rows(rows, value_input_option="RAW")
        
        safe_sheet_operation(append_operation)
        load_voters_df.clear()

def archive_and_clear_votes(election_name: str | None):
    def get_records():
        return votes_ws.get_all_records()
    rows = safe_sheet_operation(get_records)
    if not rows:
        return "no_votes"
    ts = now_cet().strftime("%Y%m%dT%H%M%S")
    safe = (election_name or "election").replace(" ", "_")[:20]
    title = f"votes_archive_{safe}_{ts}"
    
    def create_archive():
        new_ws = sheet.add_worksheet(title=title, rows=len(rows)+5, cols=3)
        new_ws.update(values=[["position","candidate","timestamp"]], range_name="A1:C1")
        new_ws.append_rows([[r["position"], r["candidate"], r["timestamp"]] for r in rows], value_input_option="RAW")
        votes_ws.clear()
        votes_ws.append_row(["position","candidate","timestamp"], value_input_option="RAW")
        return title
    
    result = safe_sheet_operation(create_archive)
    load_votes_df.clear()
    return result

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
    # অটোরিফ্রেশ সেটআপ (প্রতি 30 সেকেন্ডে)
    if is_voting_open():
        st.markdown("""
        <meta http-equiv="refresh" content="30">
        """, unsafe_allow_html=True)
    
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
                f"Start (CET): {m.get('start_cet','')}\n"
                f"End (CET): {m.get('end_cet','')}"
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
        m = meta_get_all()
        st.markdown("### 🗓️ Election control")
        st.markdown(f"- **Current election name:** `{m.get('name','(none)')}`")
        st.markdown(f"- **Status:** `{m.get('status','idle')}`")
        st.markdown(f"- **Start (CET):** `{m.get('start_cet','')}`")
        st.markdown(f"- **End (CET):** `{m.get('end_cet','')}`")
        st.markdown(f"- **Published:** `{m.get('published','FALSE')}`")

        st.divider()
        st.markdown("#### Create / Schedule new election")

        # সরলীকৃত সময় নির্বাচন - শুধুমাত্র CET তারিখ এবং সময়
        ename = st.text_input("Election name", value=m.get("name",""))
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**Start Time (CET)**")
            start_date = st.date_input("Start date", value=datetime.now(CET).date(), key="start_date")
            start_time = st.time_input("Start time", value=datetime.now(CET).time().replace(second=0, microsecond=0), key="start_time")
        
        with col2:
            st.markdown("**End Time (CET)**")
            end_date = st.date_input("End date", value=datetime.now(CET).date(), key="end_date")
            end_time = st.time_input("End time", value=(datetime.now(CET).time().replace(second=0, microsecond=0)), key="end_time")
        
        # CET তারিখ এবং সময়
        start_dt_cet = datetime.combine(start_date, start_time).replace(tzinfo=CET)
        end_dt_cet = datetime.combine(end_date, end_time).replace(tzinfo=CET)
        
        st.info(f"**সময়সূচী (CET):**\n- শুরু: {start_dt_cet.strftime('%Y-%m-%d %H:%M')}\n- শেষ: {end_dt_cet.strftime('%Y-%m-%d %H:%M')}")

        if st.button("Set & Schedule"):
            meta_set("name", ename)
            meta_set("start_cet", start_dt_cet.isoformat())
            meta_set("end_cet", end_dt_cet.isoformat())
            meta_set("status", "idle")
            meta_set("published", "FALSE")
            st.success("Election scheduled successfully!")
            st.rerun()

        c3, c4, c5 = st.columns(3)
        if c3.button("Start Election Now"):
            # Use the scheduled times if they exist, otherwise use current time to midnight
            start_cet = m.get("start_cet", "")
            end_cet = m.get("end_cet", "")
            
            if start_cet and end_cet:
                # Use the already scheduled times
                meta_set("status", "ongoing")
                st.success("Election started using scheduled times!")
            else:
                # বর্তমান CET সময় থেকে শুরু এবং আজ মধ্যরাত পর্যন্ত
                start_now = datetime.now(CET)
                end_now = start_now.replace(hour=23, minute=59, second=0)  # আজ রাত 11:59 CET পর্যন্ত
                
                meta_set("start_cet", start_now.isoformat())
                meta_set("end_cet", end_now.isoformat())
                meta_set("status", "ongoing")
                st.success("Election started now! Will end at midnight CET.")
            st.rerun()

        if c4.button("End Election Now"):
            meta_set("status", "ended")
            st.success("Election ended now.")
            st.rerun()

        if c5.button("Publish Results (declare)"):
            meta_set("published", "TRUE")
            meta_set("status", "ended")
            st.success("Results published. You can now export/archive.")
            st.rerun()

        st.divider()
        st.markdown("### 🔑 Token Generator")
        g1, g2 = st.columns(2)
        count = g1.number_input("কতটি টোকেন?", min_value=1, value=20, step=10)
        prefix = g2.text_input("Prefix", value="BYWOB-2025")
        if st.button("➕ Generate & Append"):
            try:
                generate_tokens(int(count), prefix)
                st.success(f"{int(count)}টি টোকেন voters শিটে যোগ হয়েছে।")
                st.rerun()
            except Exception as e:
                st.error(f"টোকেন তৈরি ব্যর্থ: {e}")

        st.markdown("### 👥 Voters (tokens hidden)")
        voters_df = load_voters_df()
        if voters_df.empty:
            st.info("কোনো ভোটার নেই।")
        else:
            safe = voters_df.copy()
            safe["token"] = "••••••••"
            st.dataframe(safe[["name","email","token","used","used_at"]], width="stretch")

        st.markdown("### 📋 Candidates")
        cands_df = load_candidates_df()
        if cands_df.empty:
            st.info("candidates শিট ফাঁকা। position, candidate কলামসহ ডেটা দিন।")
        else:
            st.dataframe(cands_df, width="stretch")

        st.markdown("### 📈 Tally (by position)")
        vdf = load_votes_df()
        if vdf.empty:
            st.info("এখনও কোনো ভোট পড়েনি।")
        else:
            for pos in cands_df["position"].unique():
                grp = (
                    vdf[vdf["position"] == pos]
                    .groupby("candidate")
                    .size()
                    .reset_index(name="votes")
                    .sort_values("votes", ascending=False)
                )
                if not grp.empty:
                    st.markdown(f"**{pos}**")
                    st.table(grp.set_index("candidate"))

        st.divider()
        st.markdown("### ⬇️ Export results")
        r = results_df()
        if r.empty:
            st.info("No votes yet.")
        else:
            csv_bytes = r.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download results CSV",
                data=csv_bytes,
                file_name=f"results_{meta_get_all().get('name','election')}.csv",
                mime="text/csv",
            )

        st.markdown("### 🗄️ Archive & Clear")
        if st.button("Archive votes and clear (prepare new)"):
            name_for_archive = meta_get_all().get("name","election")
            res = archive_and_clear_votes(name_for_archive)
            if res == "no_votes":
                st.info("No votes to archive.")
            else:
                st.success(f"Votes archived to sheet: {res}")
            st.rerun()
    else:
        st.warning("Please enter admin password to continue.")