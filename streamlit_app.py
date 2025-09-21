# streamlit_app.py

import random
import string
import time as pytime
from datetime import datetime, date, time as dtime, timedelta
from zoneinfo import ZoneInfo
from collections import OrderedDict

import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound
from streamlit_autorefresh import st_autorefresh

# ------------------------------------------------------------------------------
# App config
# ------------------------------------------------------------------------------
st.set_page_config(page_title="BYWOB Voting", page_icon="🗳️", layout="wide")
CET = ZoneInfo("Europe/Paris")
LIVE_REFRESH_SEC = 30       # keep >= 30 to be quota-safe
RETRY_MAX_TRIES = 3
RETRY_SLEEP_SEC = 2.0       # initial backoff

# ------------------------------------------------------------------------------
# Google Sheets connect (modern creds) + caching
# ------------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_client_and_sheet():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    sa_info = dict(st.secrets["gcp_service_account"])
    creds: Credentials = Credentials.from_service_account_info(sa_info, scopes=scope)
    client = gspread.authorize(creds)
    sheet_id = st.secrets["gcp_service_account"]["SHEET_ID"]
    sh = client.open_by_key(sheet_id)
    return creds, client, sh, sheet_id

creds, client, sh, SHEET_ID = get_client_and_sheet()

# ------------------------------------------------------------------------------
# Gentle retry wrapper (helps on transient 429s)
# ------------------------------------------------------------------------------
def gs_retry(fn, *args, **kwargs):
    tries = 0
    sleep = RETRY_SLEEP_SEC
    while True:
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            tries += 1
            if tries >= RETRY_MAX_TRIES:
                raise
            pytime.sleep(sleep)
            sleep *= 2

# ------------------------------------------------------------------------------
# Ensure worksheets & headers (executed once per session)
# ------------------------------------------------------------------------------
def ensure_worksheet(sh_, title, headers=None, rows=1000, cols=20):
    try:
        ws = gs_retry(sh_.worksheet, title)
    except WorksheetNotFound:
        ws = gs_retry(sh_.add_worksheet, title=title, rows=rows, cols=cols)
        if headers:
            gs_retry(ws.update, "A1", [headers])
    else:
        if headers:
            values = gs_retry(ws.get_all_values)
            if len(values) == 0:
                gs_retry(ws.update, "A1", [headers])
    return ws

@st.cache_resource(show_spinner=False)
def setup_structure_once(_sh):
    ensure_worksheet(_sh, "meta",      ["key", "value"])
    ensure_worksheet(_sh, "positions", ["position"])
    ensure_worksheet(_sh, "candidates",["position", "candidate"])
    ensure_worksheet(_sh, "voters",    ["name", "email", "token", "used", "used_at"])
    ensure_worksheet(_sh, "votes",     ["election_name", "position", "candidate", "token", "timestamp_cet"])
    ensure_worksheet(_sh, "results",   ["position", "candidate", "votes"])
    return True

try:
    _ = setup_structure_once(sh)
    api_ok = True
except Exception as e:
    api_ok = False
    st.error(f"❌ Google Sheet ওপেন করা যায়নি: {type(e).__name__}: {e}")

# ------------------------------------------------------------------------------
# Cached readers & meta
# ------------------------------------------------------------------------------
@st.cache_data(ttl=30, show_spinner=False)
def meta_get_all_cached(sheet_id: str):
    ws = gs_retry(sh.worksheet, "meta")
    rows = gs_retry(ws.get_all_records)
    return {r["key"]: r["value"] for r in rows}

def meta_bulk_set(kv: dict):
    ws = gs_retry(sh.worksheet, "meta")
    rows = gs_retry(ws.get_all_records)
    d = {r["key"]: r["value"] for r in rows}
    d.update(kv)
    gs_retry(ws.clear)
    gs_retry(ws.update, "A1", [["key","value"]] + [[k, d[k]] for k in d])
    meta_get_all_cached.clear()

@st.cache_data(ttl=30, show_spinner=False)
def read_df_cached(sheet_id: str, tab: str) -> pd.DataFrame:
    ws = gs_retry(sh.worksheet, tab)
    vals = gs_retry(ws.get_all_values)
    if not vals:
        return pd.DataFrame()
    return pd.DataFrame(vals[1:], columns=vals[0])

def read_df(tab: str) -> pd.DataFrame:
    return read_df_cached(SHEET_ID, tab)

# ------------------------------------------------------------------------------
# Results recompute
# ------------------------------------------------------------------------------
def write_results_from_votes():
    votes = read_df("votes")
    if votes.empty:
        df = pd.DataFrame(columns=["position","candidate","votes"])
    else:
        grp = votes.groupby(["position","candidate"]).size().reset_index(name="votes")
        df = grp.sort_values(["position","votes"], ascending=[True, False])

    ws = gs_retry(sh.worksheet, "results")
    gs_retry(ws.clear)
    if df.empty:
        gs_retry(ws.update, "A1", [["position","candidate","votes"]])
    else:
        gs_retry(ws.update, "A1", [df.columns.tolist()] + df.values.tolist())
    read_df_cached.clear()

# ------------------------------------------------------------------------------
# Voting helpers
# ------------------------------------------------------------------------------
def validate_token(token_str: str):
    voters = read_df("voters")
    if voters.empty:
        return False, None
    t = token_str.strip()
    row = voters[voters["token"].str.strip().str.upper() == t.upper()]
    if row.empty:
        return False, None
    used = str(row.iloc[0].get("used","")).strip().lower()
    if used in ("true","1","yes","y"):
        return False, None
    return True, row.index[0]

def mark_token_used(df_index: int):
    ws = gs_retry(sh.worksheet, "voters")
    row_num = df_index + 2
    used_col = 4
    used_at_col = 5
    now_cet = datetime.now(CET).isoformat()
    gs_retry(ws.update_cell, row_num, used_col, "TRUE")
    gs_retry(ws.update_cell, row_num, used_at_col, now_cet)
    read_df_cached.clear()

def generate_tokens(count: int, prefix: str):
    alphabet = string.ascii_uppercase + string.digits
    return [f"{prefix}{''.join(random.choices(alphabet, k=6))}" for _ in range(count)]

# ------------------------------------------------------------------------------
# UI: header & tabs
# ------------------------------------------------------------------------------
st.title("🗳️ BYWOB Online Voting")
with st.sidebar:
    st.caption(f"SA: {creds.service_account_email}")
    st.caption(f"Project: {creds.project_id}")
    st.caption(f"Sheet ID: {SHEET_ID}")

tabs = st.tabs(["Vote", "Results", "Admin"])

# ------------------------------------------------------------------------------
# VOTE TAB
# ------------------------------------------------------------------------------
with tabs[0]:
    st.header("🗳️ Vote")

    if not api_ok:
        st.info("Sheets API ঠিক না হওয়া পর্যন্ত ভোট দেওয়া যাবে না।")
    else:
        meta = meta_get_all_cached(SHEET_ID)
        status = meta.get("status","pending").lower()
        ename  = meta.get("name","(unnamed)")

        def parse_dt(s):
            try: return datetime.fromisoformat(s)
            except: return None

        start_dt = parse_dt(meta.get("start_cet",""))
        end_dt   = parse_dt(meta.get("end_cet",""))
        now_cet  = datetime.now(CET)

        st.markdown(f"**Election:** `{ename}`  •  **Status:** `{status}`")
        if start_dt: st.caption(f"Starts: {start_dt} CET")
        if end_dt:   st.caption(f"Ends  : {end_dt} CET")

        allowed = False
        if status == "ongoing":
            allowed = True
        elif status in ("scheduled","pending"):
            if start_dt and start_dt <= now_cet and (not end_dt or now_cet <= end_dt):
                allowed = True

        if not allowed:
            st.warning("⏳ এখন ভোটিং উইন্ডো খোলা নেই। সময়/স্ট্যাটাস চেক করুন।")
        else:
            token = st.text_input("Enter your voting token")
            if st.button("Proceed"):
                valid, df_index = validate_token(token)
                if not valid:
                    st.error("❌ Invalid or already used token.")
                else:
                    # -------- Ballot exactly like before: from 'candidates' sheet, keep sheet order
                    cand_df = read_df("candidates").copy()
                    if cand_df.empty:
                        st.error("প্রার্থীদের তালিকা খালি। Admin থেকে যোগ করুন।")
                    else:
                        cand_df["position"]  = cand_df["position"].astype(str).str.strip()
                        cand_df["candidate"] = cand_df["candidate"].astype(str).str.strip()
                        cand_df = cand_df.replace({"": pd.NA}).dropna(subset=["position","candidate"])

                        ballot_map = OrderedDict()
                        for _, r in cand_df.iterrows():
                            p = r["position"]; c = r["candidate"]
                            ballot_map.setdefault(p, []).append(c)

                        st.success("✅ Token OK. Select your choices below.")
                        with st.form("full_ballot"):
                            selections = {}
                            for idx, (p, candidates) in enumerate(ballot_map.items()):
                                choice = st.radio(
                                    f"{p}",
                                    options=candidates,
                                    horizontal=True,
                                    key=f"ballot_radio_{idx}_{p}"
                                )
                                selections[p] = choice

                            submitted = st.form_submit_button("Submit All Votes")
                            if submitted:
                                ws_votes = gs_retry(sh.worksheet, "votes")
                                ts = datetime.now(CET).isoformat()
                                rows = [[ename, p, c, token.strip(), ts] for p, c in selections.items()]
                                if rows:
                                    gs_retry(ws_votes.append_rows, rows)
                                    mark_token_used(df_index)
                                    write_results_from_votes()
                                    st.success("✅ Your vote has been recorded.")
                                else:
                                    st.error("কোনো নির্বাচন করা হয়নি।")

# ------------------------------------------------------------------------------
# RESULTS TAB
# ------------------------------------------------------------------------------
with tabs[1]:
    st.header("📊 Results (Live)")
    if not api_ok:
        st.info("Sheets API ঠিক না হওয়া পর্যন্ত ফলাফল দেখা যাবে না।")
    else:
        auto = st.toggle(
            f"Auto refresh every {LIVE_REFRESH_SEC}s",
            value=False,
            help="Auto refresh চালু করলে API কোটায় চাপ পড়তে পারে।",
        )
        if auto:
            st_autorefresh(interval=LIVE_REFRESH_SEC * 1000, key="auto_refresh")

        col, _ = st.columns([1,3])
        if col.button("🔄 Refresh now"):
            read_df_cached.clear()
            st.rerun()

        res_df = read_df("results")
        if res_df.empty:
            st.info("এখনো কোনো ভোট পড়েনি বা ফলাফল তৈরি হয়নি। Admin ট্যাব থেকে Tally/Refresh দিন।")
        else:
            st.dataframe(res_df, use_container_width=True)

        st.caption("টিপ: খুব ঘনঘন রিফ্রেশ করবেন না—429 quota exceeded এড়াতে।")

# ------------------------------------------------------------------------------
# ADMIN TAB (minimal UI, no big tables)
# ------------------------------------------------------------------------------
with tabs[2]:
    st.header("👨‍💻 Admin")

    if not api_ok:
        st.info("Sheets API ফিক্স করুন—Secrets/Permissions/Sheet ID চেক করুন।")
    else:
        meta = meta_get_all_cached(SHEET_ID)
        st.subheader("Election Setup (CET)")
        ename = st.text_input("Election name", value=meta.get("name","BYWOB Election"))

        now_cet = datetime.now(CET)

        def dparse(s, fb):
            try: return date.fromisoformat(s)
            except: return fb

        def tparse(s, fb):
            try:
                hh, mm = s.split(":")
                return dtime(int(hh), int(mm))
            except:
                return fb

        default_start_date = dparse(meta.get("start_date_cet",""), now_cet.date())
        default_end_date   = dparse(meta.get("end_date_cet",""), (now_cet + timedelta(days=1)).date())
        default_start_time = tparse(meta.get("start_time_cet","09:00"), dtime(9,0))
        default_end_time   = tparse(meta.get("end_time_cet","18:00"), dtime(18,0))

        c1, c2 = st.columns(2)
        start_date = c1.date_input("Start date (CET)", value=default_start_date)
        start_time = c1.time_input("Start time (CET)", value=default_start_time)
        end_date   = c2.date_input("End date (CET)",   value=default_end_date)
        end_time   = c2.time_input("End time (CET)",   value=default_end_time)

        start_dt_cet = datetime.combine(start_date, start_time, tzinfo=CET)
        end_dt_cet   = datetime.combine(end_date,   end_time,   tzinfo=CET)

        c3, c4, c5 = st.columns([1,1,2])

        if c3.button("💾 Save Config"):
            meta_bulk_set({
                "name": ename,
                "start_date_cet": start_date.isoformat(),
                "start_time_cet": start_time.strftime("%H:%M"),
                "end_date_cet": end_date.isoformat(),
                "end_time_cet": end_time.strftime("%H:%M"),
                "start_cet": start_dt_cet.isoformat(),
                "end_cet": end_dt_cet.isoformat(),
                "status": "scheduled",
            })
            st.success("Config saved (status=scheduled).")

        if c4.button("▶️ Start Election Now"):
            now_cet = datetime.now(CET)
            meta_bulk_set({
                "name": ename,
                "start_date_cet": now_cet.date().isoformat(),
                "start_time_cet": now_cet.strftime("%H:%M"),
                "start_cet": now_cet.isoformat(),
                "status": "ongoing",
            })
            st.success(f"Election started at {now_cet} CET (status=ongoing).")

        if c5.button("⏹ End Election Now"):
            now_cet = datetime.now(CET)
            meta_bulk_set({
                "end_date_cet": now_cet.date().isoformat(),
                "end_time_cet": now_cet.strftime("%H:%M"),
                "end_cet": now_cet.isoformat(),
                "status": "ended",
            })
            st.warning(f"Election ended at {now_cet} CET (status=ended).")

        st.divider()
        with st.expander("➕ Quick add (positions & candidates)"):
            cc1, cc2, cc3 = st.columns([2,2,1])
            new_pos = cc1.text_input("Add position")
            if cc3.button("Add position"):
                if new_pos.strip():
                    ws = gs_retry(sh.worksheet, "positions")
                    gs_retry(ws.append_row, [new_pos.strip()])
                    read_df_cached.clear()
                    st.success("Position added.")

            # Use current positions for candidate add
            pos_df = read_df("positions")
            pos_options = pos_df["position"].tolist() if not pos_df.empty else []
            new_pos2 = cc1.selectbox("Position for candidate", options=pos_options)
            new_cand = cc2.text_input("Candidate name")
            if cc3.button("Add candidate"):
                if new_pos2 and new_cand.strip():
                    ws = gs_retry(sh.worksheet, "candidates")
                    gs_retry(ws.append_row, [new_pos2, new_cand.strip()])
                    read_df_cached.clear()
                    st.success("Candidate added.")

        st.divider()
        with st.expander("🔑 Token Generator"):
            t1, t2, t3 = st.columns([1,1,1])
            count = t1.number_input("How many", min_value=1, max_value=2000, value=50, step=10)
            prefix = t2.text_input("Prefix", value="BYWOB-2025-")
            if t3.button("Generate & Append"):
                toks = generate_tokens(count, prefix)
                rows = [["", "", tok, "FALSE", ""] for tok in toks]
                ws = gs_retry(sh.worksheet, "voters")
                gs_retry(ws.append_rows, rows)
                read_df_cached.clear()
                st.success(f"{len(toks)} tokens appended.")

        st.divider()
        cta1, cta2, _ = st.columns([1,1,2])
        if cta1.button("🧮 Recompute tally"):
            write_results_from_votes()
            st.success("Results recomputed from votes.")
        if cta2.button("📦 Archive previous votes & reset tokens"):
            ts = datetime.now(CET).strftime("%Y%m%d_%H%M%S")
            votes_df = read_df("votes")
            if not votes_df.empty:
                ws_arch = gs_retry(sh.add_worksheet, f"votes_archive_{ts}", rows=2, cols=max(5, votes_df.shape[1]))
                gs_retry(ws_arch.update, "A1", [votes_df.columns.tolist()] + votes_df.values.tolist())
                ws_votes = gs_retry(sh.worksheet, "votes")
                gs_retry(ws_votes.clear)
                gs_retry(ws_votes.update, "A1", [["election_name","position","candidate","token","timestamp_cet"]])
            # reset voters flags
            vws = gs_retry(sh.worksheet, "voters")
            vals = gs_retry(vws.get_all_values)
            if vals:
                header = vals[0]
                used_idx = header.index("used")
                used_at_idx = header.index("used_at")
                for r in range(1, len(vals)):
                    if len(vals[r]) < len(header):
                        vals[r] += [""] * (len(header) - len(vals[r]))
                    vals[r][used_idx] = "FALSE"
                    vals[r][used_at_idx] = ""
                gs_retry(vws.clear)
                gs_retry(vws.update, "A1", vals)
            read_df_cached.clear()
            st.success("Archived old votes & reset tokens.")

        st.divider()
        with st.expander("🧰 Diagnostics"):
            st.write("Service Account:", st.secrets["gcp_service_account"]["client_email"])
            st.write("Project ID:", st.secrets["gcp_service_account"]["project_id"])
            try:
                _ = gs_retry(sh.worksheets)
                st.success("Sheets API reachable ✅")
            except Exception as e:
                st.error(f"Sheets API problem: {e}")

        st.divider()
        with st.expander("📋 Operator Guide"):
            st.markdown("""
**দ্রুত গাইড (CET):**
1) `candidates` শিটে প্রতিটি **position**–এর নিচে **candidate** নামগুলো দিন (শিটের অর্ডারই UI তে দেখাবে)।  
2) টোকেন জেনারেট করুন (voters শিটে যাবে)।  
3) Election name + Start/End (CET) সেট করে **Save Config** দিন।  
4) সময় হলে নিজে থেকেই ভোট খুলবে, না হলে **Start Election Now** চাপুন।  
5) **Results** ট্যাবে Refresh বা Auto refresh (≥30s) ব্যবহার করুন।  
6) শেষে **End Election Now** / সময় শেষে স্বয়ংক্রিয়ভাবে বন্ধ।  
7) নতুন নির্বাচন শুরুর আগে **Archive previous votes & reset tokens** দিন।
""")
