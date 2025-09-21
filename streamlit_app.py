# BYWOB Online Voting — Streamlit + Google Sheets
# - Uses google-auth (no oauth2client)
# - CET/CEST (Europe/Paris) time; stores start/end in CET
# - Server-wide cache + exponential backoff to reduce 429s
# - One-screen voting (all positions), live results, full admin tools

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials

# ------------- App & Timezone -------------
LOCAL_TZ = ZoneInfo("Europe/Paris")  # CET/CEST
st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="centered")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit + Google Sheets • One-time tokens • Timezone: CET/CEST (Europe/Paris)")

# ------------- Quota-friendly cache TTLs -------------
LIVE_REFRESH_SEC = 30    # Results auto-refresh interval (when toggled ON)
TTL_VOTERS_SEC   = 600   # voters cache 10 min
TTL_CANDS_SEC    = 600   # candidates cache 10 min
TTL_VOTES_SEC    = 60    # votes cache 1 min

# ------------- Secrets & Auth -------------
if "gcp_service_account" not in st.secrets:
    st.error("Secrets missing: [gcp_service_account]. App Settings → Secrets-এ Service Account JSON + SHEET_ID দিন।")
    st.stop()

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
sa_info = dict(st.secrets["gcp_service_account"])
creds: Credentials = Credentials.from_service_account_info(sa_info, scopes=scope)
client = gspread.authorize(creds)

try:
    SHEET_ID = st.secrets["gcp_service_account"]["SHEET_ID"]
    sheet = client.open_by_key(SHEET_ID)
except Exception as e:
    st.error(f"❌ Google Sheet ওপেন করা যায়নি: {e}")
    st.stop()

# ------------- Worksheet helpers -------------
def ensure_ws(title: str, headers: list[str], rows: int = 100, cols: int = 10):
    try:
        ws = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=title, rows=rows, cols=cols)
        end_col = chr(64 + len(headers))
        ws.update(range_name=f"A1:{end_col}1", values=[headers])
    return sheet.worksheet(title)

meta_ws       = ensure_ws("meta", ["key", "value"], rows=20, cols=2)
voters_ws     = ensure_ws("voters", ["name", "email", "token", "used", "used_at"], rows=2000, cols=5)
candidates_ws = ensure_ws("candidates", ["position", "candidate"], rows=500, cols=2)
votes_ws      = ensure_ws("votes", ["position", "candidate", "timestamp"], rows=5000, cols=3)

# ------------- Safe read with backoff -------------
def safe_get_all_records(ws, max_retries=5, base_sleep=1.0):
    """429/Quota exceeded এলে exponential backoff সহ রিট্রাই করবে."""
    for i in range(max_retries):
        try:
            return ws.get_all_records()
        except APIError as e:
            msg = str(e)
            if "429" in msg or "Quota exceeded" in msg:
                time.sleep(base_sleep * (2 ** i))  # 1s,2s,4s,8s,16s
                continue
            raise
    return ws.get_all_records()

# ------------- Meta helpers -------------
def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

def meta_get_all() -> dict:
    recs = safe_get_all_records(meta_ws)
    return {r.get("key"): r.get("value") for r in recs if r.get("key")}

def meta_set(key: str, value: str):
    recs = meta_ws.get_all_records()
    for i, r in enumerate(recs, start=2):
        if r.get("key") == key:
            meta_ws.update_cell(i, 2, value)
            return
    meta_ws.append_row([key, value], value_input_option="RAW")

# defaults (first run + backward compatibility)
_m = meta_get_all()
if "status" not in _m:     meta_set("status", "idle")
if "name" not in _m:       meta_set("name", "")
if "start_utc" in _m and "start_cet" not in _m:
    meta_set("start_cet", _m.get("start_utc"))
    meta_set("end_cet", _m.get("end_utc", ""))
if "start_cet" not in _m:  meta_set("start_cet", "")
if "end_cet" not in _m:    meta_set("end_cet", "")
if "published" not in _m:  meta_set("published", "FALSE")

def parse_iso_local_or_none(s: str | None):
    if not s: return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ)
    except Exception:
        return None

def is_voting_open() -> bool:
    m = meta_get_all()
    if m.get("status", "idle") != "ongoing":
        return False
    start_dt = parse_iso_local_or_none(m.get("start_cet"))
    end_dt   = parse_iso_local_or_none(m.get("end_cet"))
    now = now_local()
    if start_dt and now < start_dt:
        return False
    if end_dt and now > end_dt:
        meta_set("status", "ended")
        return False
    return True

# ------------- Server-wide sheet cache -------------
@st.cache_resource
def get_sheet_cache():
    from dataclasses import dataclass
    from time import time as _time

    @dataclass
    class _Item:
        df: pd.DataFrame | None
        ts: float

    class SheetCache:
        def __init__(self):
            self._voters = _Item(None, 0.0)
            self._cands  = _Item(None, 0.0)
            self._votes  = _Item(None, 0.0)

        def _expired(self, last_ts: float, ttl: int) -> bool:
            return (_time() - last_ts) > ttl

        def voters_df(self) -> pd.DataFrame:
            import pandas as _pd
            if self._voters.df is None or self._expired(self._voters.ts, TTL_VOTERS_SEC):
                df = _pd.DataFrame(safe_get_all_records(voters_ws))
                if df.empty:
                    df = _pd.DataFrame(columns=["name","email","token","used","used_at"])
                for c in ["name","email","token","used","used_at"]:
                    if c not in df.columns: df[c] = ""
                df["token"] = df["token"].astype(str).str.strip()
                df["used_bool"] = df["used"].astype(str).str.strip().str.lower().isin(["true","1","yes"])
                self._voters = _Item(df, _time())
            return self._voters.df

        def candidates_df(self) -> pd.DataFrame:
            import pandas as _pd
            if self._cands.df is None or self._expired(self._cands.ts, TTL_CANDS_SEC):
                df = _pd.DataFrame(safe_get_all_records(candidates_ws))
                if df.empty:
                    df = _pd.DataFrame(columns=["position","candidate"])
                for c in ["position","candidate"]:
                    if c not in df.columns: df[c] = ""
                df["position"]  = df["position"].astype(str).str.strip()
                df["candidate"] = df["candidate"].astype(str).str.strip()
                df = df[(df["position"]!="") & (df["candidate"]!="")]
                self._cands = _Item(df[["position","candidate"]], _time())
            return self._cands.df

        def votes_df(self) -> pd.DataFrame:
            import pandas as _pd
            if self._votes.df is None or self._expired(self._votes.ts, TTL_VOTES_SEC):
                df = _pd.DataFrame(safe_get_all_records(votes_ws))
                if df.empty:
                    df = _pd.DataFrame(columns=["position","candidate","timestamp"])
                self._votes = _Item(df[["position","candidate","timestamp"]], _time())
            return self._votes.df

        def invalidate_voters(self): self._voters.ts = 0.0
        def invalidate_cands(self):  self._cands.ts  = 0.0
        def invalidate_votes(self):  self._votes.ts  = 0.0

    return SheetCache()

SHEET_CACHE = get_sheet_cache()

def load_voters_df():     return SHEET_CACHE.voters_df()
def load_candidates_df(): return SHEET_CACHE.candidates_df()
def load_votes_df():      return SHEET_CACHE.votes_df()
def clear_caches():
    SHEET_CACHE.invalidate_voters()
    SHEET_CACHE.invalidate_cands()
    SHEET_CACHE.invalidate_votes()

# ------------- Sheet writes -------------
def mark_token_used(voters_df: pd.DataFrame, token: str):
    t = str(token).strip()
    m = voters_df[voters_df["token"] == t]
    if m.empty: return
    row_idx = m.index[0] + 2
    voters_ws.update_cell(row_idx, 4, "TRUE")
    voters_ws.update_cell(row_idx, 5, now_local().isoformat())  # CET timestamp
    SHEET_CACHE.invalidate_voters()

def append_vote_rows(rows: list[list[str]]):
    if rows:
        votes_ws.append_rows(rows, value_input_option="RAW")
        SHEET_CACHE.invalidate_votes()

def generate_tokens(n: int, prefix: str):
    import secrets, string
    alpha = string.ascii_uppercase + string.digits
    rows = []
    for _ in range(int(n)):
        tok = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        rows.append(["","",tok,"FALSE",""])
    if rows:
        voters_ws.append_rows(rows, value_input_option="RAW")
        SHEET_CACHE.invalidate_voters()

def archive_votes(election_name: str | None):
    rows = safe_get_all_records(votes_ws)
    if not rows: return None
    ts = now_local().strftime("%Y%m%dT%H%M%S%z")
    safe = (election_name or "election").replace(" ", "_")[:20]
    title = f"votes_archive_{safe}_{ts}"
    new_ws = sheet.add_worksheet(title=title, rows=len(rows)+5, cols=3)
    new_ws.update(range_name="A1:C1", values=[["position","candidate","timestamp"]])
    new_ws.append_rows([[r["position"], r["candidate"], r["timestamp"]] for r in rows], value_input_option="RAW")
    return title

def clear_votes_sheet():
    votes_ws.clear()
    votes_ws.append_row(["position","candidate","timestamp"], value_input_option="RAW")
    SHEET_CACHE.invalidate_votes()

def reset_all_tokens():
    df = load_voters_df()
    if df.empty: return 0
    for i in range(len(df)):
        row_idx = i + 2
        voters_ws.update_cell(row_idx, 4, "FALSE")
        voters_ws.update_cell(row_idx, 5, "")
    SHEET_CACHE.invalidate_voters()
    return len(df)

def results_df():
    df = load_votes_df()
    if df.empty:
        return pd.DataFrame(columns=["position","candidate","votes"])
    out = df.groupby(["position","candidate"]).size().reset_index(name="votes")
    return out.sort_values(["position","votes"], ascending=[True, False])

# ------------- UI Tabs -------------
tab_vote, tab_results, tab_admin = st.tabs(["🗳️ Vote", "📊 Results", "🔑 Admin"])

# ------------- Vote (all positions together) -------------
with tab_vote:
    st.subheader("ভোট দিন (টোকেন ব্যবহার করে)")
    token = st.text_input("আপনার টোকেন লিখুন", placeholder="BYWOB-2025-XXXXXX")

    if st.button("Proceed"):
        if not token:
            st.error("টোকেন দিন।"); st.stop()

        if not is_voting_open():
            m = meta_get_all()
            st.error(
                "এখন ভোট গ্রহণ করা হচ্ছে না।\n\n"
                f"Status: {m.get('status','idle')}\n"
                f"Start (CET): {m.get('start_cet','')}\n"
                f"End (CET): {m.get('end_cet','')}"
            ); st.stop()

        voters = load_voters_df()
        row = voters[voters["token"] == token.strip()]
        if row.empty: st.error("❌ টোকেন সঠিক নয়।"); st.stop()
        if row["used_bool"].iloc[0]: st.error("⚠️ এই টোকেনটি ইতিমধ্যে ব্যবহার করা হয়েছে।"); st.stop()

        cands = load_candidates_df()
        if cands.empty:
            st.warning("candidates শিট ফাঁকা। Admin ট্যাব থেকে প্রার্থী যোগ করুন।"); st.stop()

        positions = sorted(cands["position"].unique().tolist())
        pos_to_cands = {p: cands[cands["position"] == p]["candidate"].tolist() for p in positions}

        st.markdown("### সব পদের জন্য আপনার পছন্দ বাছাই করুন")
        with st.form("all_positions_form", clear_on_submit=False):
            selections = {}
            for p in positions:
                selections[p] = st.radio(f"**{p}**", options=pos_to_cands[p], index=0, key=f"pick_{p}")
            submitted = st.form_submit_button("✅ Submit All Votes")

        if submitted:
            rows = []
            ts = now_local().isoformat()  # CET timestamp
            for p, cand in selections.items():
                if cand and cand.strip():
                    rows.append([p, cand, ts])

            if not rows:
                st.error("কমপক্ষে একটি পদের জন্য প্রার্থী সিলেক্ট করুন।"); st.stop()

            append_vote_rows(rows)
            mark_token_used(voters, token)
            st.success("আপনার সব ভোট গ্রহণ করা হয়েছে। ধন্যবাদ!")

# ------------- Results -------------
with tab_results:
    st.subheader("📊 Live Results")

    auto = st.toggle(f"Auto refresh every {LIVE_REFRESH_SEC}s", value=False)
    if auto:
        st.autorefresh(interval=LIVE_REFRESH_SEC * 1000, key="auto_refresh_key")

    c1, _ = st.columns([1, 3])
    if c1.button("🔄 Refresh now"):
        clear_caches()
        st.rerun()

    r = results_df()
    if r.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
    else:
        st.dataframe(r, width="stretch")

# ------------- Admin -------------
with tab_admin:
    st.subheader("🛠️ Admin Tools")

    # Optional password
    ok = True
    pw_secret = st.secrets.get("ADMIN_PASSWORD")
    if pw_secret:
        given = st.text_input("Admin password", type="password")
        ok = (given == pw_secret)
        if given and not ok: st.error("Wrong password")
    if not ok:
        st.warning("Please enter admin password to continue.")
        st.stop()

    # Diagnostics (which SA/project is running)
    with st.expander("🔎 Diagnostics (Service Account)"):
        try:
            st.write("**Service Account Email:**", creds.service_account_email)
        except Exception:
            st.write("**Service Account Email:** (unavailable)")
        st.write("**Configured Project ID:**", st.secrets["gcp_service_account"].get("project_id"))
        st.write("**Key ID:**", st.secrets["gcp_service_account"].get("private_key_id"))
        try:
            st.success(f"Sheets API reachable ✅ (Spreadsheet: {sheet.title})")
        except Exception as e:
            st.error(f"Sheets API error: {e}")

    m = meta_get_all()
    start_dt_meta = parse_iso_local_or_none(m.get("start_cet"))
    end_dt_meta   = parse_iso_local_or_none(m.get("end_cet"))
    now_ = now_local()

    st.markdown("### 🗓️ Election control")
    st.markdown(f"- **Current election name:** `{m.get('name','(none)')}`")
    st.markdown(f"- **Status:** `{m.get('status','idle')}`")
    st.markdown(f"- **Start (CET):** `{m.get('start_cet','')}`")
    st.markdown(f"- **End (CET):** `{m.get('end_cet','')}`")
    st.markdown(f"- **Published:** `{m.get('published','FALSE')}`")

    st.divider()
    st.markdown("#### Create / Schedule new election (CET/CEST)")

    c1, c2 = st.columns(2)
    start_date_default = (start_dt_meta or now_).date()
    end_date_default   = (end_dt_meta   or now_).date()
    start_time_default = (start_dt_meta or now_).time().replace(microsecond=0)
    end_time_default   = (end_dt_meta   or now_).time().replace(microsecond=0)

    start_date = c1.date_input("Start date (CET)", value=start_date_default)
    end_date   = c2.date_input("End date (CET)",   value=end_date_default)

    mode = st.radio("Time input mode",
                    ["Picker (recommended)", "Manual (type HH:MM or HH:MM:SS)"],
                    horizontal=True)

    if mode == "Picker (recommended)":
        start_time = c1.time_input("Start time (CET)", value=start_time_default,
                                   step=timedelta(minutes=1))
        end_time   = c2.time_input("End time (CET)",   value=end_time_default,
                                   step=timedelta(minutes=1))
    else:
        st.caption("Tip: 24h format, যেমন 09:05 বা 09:05:30")
        cc1, cc2 = st.columns(2)
        s_str = cc1.text_input("Start time (CET) — manual", value=start_time_default.strftime("%H:%M:%S"))
        e_str = cc2.text_input("End time (CET) — manual",   value=end_time_default.strftime("%H:%M:%S"))
        def _p(s):
            for fmt in ("%H:%M:%S", "%H:%M"):
                try: return datetime.strptime(s.strip(), fmt).time()
                except ValueError: pass
            raise ValueError("Invalid time format. Use HH:MM or HH:MM:SS")
        try:
            start_time = _p(s_str); end_time = _p(e_str)
        except ValueError as e:
            st.error(str(e)); st.stop()

    start_dt = datetime.combine(start_date, start_time).replace(tzinfo=LOCAL_TZ)
    end_dt   = datetime.combine(end_date,   end_time).replace(tzinfo=LOCAL_TZ)
    ename = st.text_input("Election name", value=m.get("name",""))

    if st.button("Set & Schedule"):
        meta_set("name", ename)
        meta_set("start_cet", start_dt.isoformat())
        meta_set("end_cet", end_dt.isoformat())
        meta_set("status", "idle")
        meta_set("published", "FALSE")
        st.success(f"Election scheduled (CET).\nStart: {start_dt.isoformat()}\nEnd: {end_dt.isoformat()}")
        st.rerun()

    c3, c4, c5 = st.columns(3)
    if c3.button("Start Election Now"):
        meta_set("status", "ongoing")
        meta_set("start_cet", now_.isoformat())
        st.success("Election started. Start time set to now (CET).")
        st.rerun()

    if c4.button("End Election Now"):
        meta_set("status", "ended")
        meta_set("end_cet", now_.isoformat())
        st.success("Election ended. End time set to now (CET).")
        st.rerun()

    if c5.button("Publish Results (declare)"):
        meta_set("published", "TRUE"); meta_set("status", "ended")
        st.success("Results published.")
        st.rerun()

    st.divider()
    st.markdown("### 🔄 Start New Election (Archive & Reset)")
    st.caption("আগের ভোটগুলো আলাদা শিটে সংরক্ষণ হবে, votes ক্লিয়ার হবে, সব টোকেন আবার ব্যবহারযোগ্য হবে।")
    if st.button("Archive previous votes & reset tokens"):
        archived_title = archive_votes(meta_get_all().get("name","election"))
        clear_votes_sheet()
        n = reset_all_tokens()
        if archived_title:
            st.success(f"Votes archived to sheet: {archived_title}")
        else:
            st.info("No previous votes to archive.")
        st.success(f"Tokens reset: {n} rows updated (used=FALSE).")

    st.divider()
    st.markdown("### 🔑 Token Generator")
    g1, g2 = st.columns(2)
    count  = g1.number_input("কতটি টোকেন?", min_value=1, value=20, step=10)
    prefix = g2.text_input("Prefix", value="BYWOB-2025")
    if st.button("➕ Generate & Append"):
        try:
            generate_tokens(int(count), prefix)
            st.success(f"{int(count)}টি টোকেন voters শিটে যোগ হয়েছে।")
        except Exception as e:
            st.error(f"টোকেন তৈরি ব্যর্থ: {e}")

    st.markdown("### 👥 Voters (tokens hidden)")
    vdf = load_voters_df()
    if vdf.empty:
        st.info("কোনো ভোটার নেই।")
    else:
        safe = vdf.copy(); safe["token"] = "••••••••"
        st.dataframe(safe[["name","email","token","used","used_at"]], width="stretch")

    st.markdown("### 📋 Candidates")
    cdf = load_candidates_df()
    if cdf.empty:
        st.info("candidates শিট ফাঁকা। position, candidate কলামসহ ডেটা দিন।")
    else:
        st.dataframe(cdf, width="stretch")

    st.markdown("### 📈 Tally (by position)")
    vts = load_votes_df()
    if vts.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
    else:
        for pos in cdf["position"].unique():
            grp = (
                vts[vts["position"] == pos]
                .groupby("candidate").size().reset_index(name="votes")
                .sort_values("votes", ascending=False)
            )
            if not grp.empty:
                st.markdown(f"**{pos}**")
                st.table(grp.set_index("candidate"))

    st.divider()
    st.markdown("### ⬇️ Export results (CSV)")
    res = results_df()
    if res.empty:
        st.info("No votes yet.")
    else:
        st.download_button(
            "Download results CSV",
            data=res.to_csv(index=False).encode("utf-8"),
            file_name=f"results_{meta_get_all().get('name','election')}.csv",
            mime="text/csv",
        )
