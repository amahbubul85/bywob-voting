# streamlit_app.py

import random
import string
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from streamlit_autorefresh import st_autorefresh

# ------------------------------------------------------------------------------
# App Config
# ------------------------------------------------------------------------------
st.set_page_config(page_title="BYWOB Voting", page_icon="🗳️", layout="wide")
CET = ZoneInfo("Europe/Paris")
LIVE_REFRESH_SEC = 15  # Auto refresh interval (seconds). Keep >= 10 to avoid API quota hits.

# ------------------------------------------------------------------------------
# Google Sheets Connect (modern creds)
# ------------------------------------------------------------------------------
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
sa_info = dict(st.secrets["gcp_service_account"])
creds: Credentials = Credentials.from_service_account_info(sa_info, scopes=scope)
client = gspread.authorize(creds)

SHEET_ID = st.secrets["gcp_service_account"]["SHEET_ID"]

def open_sheet():
    return client.open_by_key(SHEET_ID)

# ------------------------------------------------------------------------------
# Helpers: ensure worksheets & headers
# ------------------------------------------------------------------------------
def ensure_worksheet(sh, title, headers=None, rows=1000, cols=20):
    try:
        ws = sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        if headers:
            ws.update("A1", [headers])
    else:
        if headers:
            # If empty, write headers
            values = ws.get_all_values()
            if len(values) == 0:
                ws.update("A1", [headers])
    return ws

def setup_structure():
    sh = open_sheet()
    ensure_worksheet(sh, "meta", ["key", "value"])
    ensure_worksheet(sh, "positions", ["position"])
    ensure_worksheet(sh, "candidates", ["position", "candidate"])
    ensure_worksheet(sh, "voters", ["name", "email", "token", "used", "used_at"])
    ensure_worksheet(sh, "votes", ["election_name", "position", "candidate", "token", "timestamp_cet"])
    ensure_worksheet(sh, "results", ["position", "candidate", "votes"])
    return sh

def meta_get_all(sh):
    ws = sh.worksheet("meta")
    rows = ws.get_all_records()
    return {r["key"]: r["value"] for r in rows}

def meta_set(sh, key, value):
    ws = sh.worksheet("meta")
    rows = ws.get_all_records()
    d = {r["key"]: r["value"] for r in rows}
    d[key] = value
    # rewrite from scratch (simple)
    ws.clear()
    ws.update("A1", [["key","value"]]+[[k, d[k]] for k in d])

def meta_bulk_set(sh, kv: dict):
    ws = sh.worksheet("meta")
    rows = ws.get_all_records()
    d = {r["key"]: r["value"] for r in rows}
    d.update(kv)
    ws.clear()
    ws.update("A1", [["key","value"]]+[[k, d[k]] for k in d])

def read_df(sh, tab):
    ws = sh.worksheet(tab)
    vals = ws.get_all_values()
    if not vals:
        return pd.DataFrame()
    df = pd.DataFrame(vals[1:], columns=vals[0])
    return df

def write_results_from_votes(sh):
    """Aggregate votes -> results sheet."""
    votes = read_df(sh, "votes")
    if votes.empty:
        df = pd.DataFrame(columns=["position","candidate","votes"])
    else:
        grp = votes.groupby(["position","candidate"]).size().reset_index(name="votes")
        df = grp.sort_values(["position","votes"], ascending=[True, False])
    ws = sh.worksheet("results")
    ws.clear()
    ws.update("A1", [df.columns.tolist()] + df.values.tolist() if not df.empty else [["position","candidate","votes"]])

def validate_token(sh, token_str):
    voters = read_df(sh, "voters")
    if voters.empty:
        return False, None
    # Clean compare
    t = token_str.strip()
    row = voters[voters["token"].str.strip().str.upper() == t.upper()]
    if row.empty:
        return False, None
    used = str(row.iloc[0].get("used","")).strip().lower()
    if used in ("true","1","yes","y"):
        return False, None
    return True, row.index[0]  # return the dataframe index (0-based excluding header)

def mark_token_used(sh, df_index):
    ws = sh.worksheet("voters")
    # gspread rows start at 1, header at 1, so index in df + 2
    row_num = df_index + 2
    used_col = 4
    used_at_col = 5
    now_cet = datetime.now(CET).isoformat()
    ws.update_cell(row_num, used_col, "TRUE")
    ws.update_cell(row_num, used_at_col, now_cet)

def generate_tokens(count: int, prefix: str):
    tokens = []
    for _ in range(count):
        suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        tokens.append(f"{prefix}{suffix}")
    return tokens

# ------------------------------------------------------------------------------
# UI: Header & Tabs
# ------------------------------------------------------------------------------
st.title("🗳️ BYWOB Voting Platform")

with st.sidebar:
    st.markdown("**Service Account:**")
    st.code(st.secrets["gcp_service_account"]["client_email"], language=None)
    st.markdown("**Project:**")
    st.code(st.secrets["gcp_service_account"]["project_id"], language=None)
    st.markdown("**Sheet ID:**")
    st.code(SHEET_ID, language=None)

try:
    sh = setup_structure()
    api_ok = True
except Exception as e:
    api_ok = False
    st.error(f"❌ Google Sheet ওপেন করা যায়নি: {type(e).__name__}: {e}")

tabs = st.tabs(["Vote", "Results", "Admin"])

# ------------------------------------------------------------------------------
# VOTE TAB
# ------------------------------------------------------------------------------
with tabs[0]:
    st.header("🗳️ Vote")
    if not api_ok:
        st.info("Sheets API ঠিক না হওয়া পর্যন্ত ভোট দেওয়া যাবে না।")
    else:
        meta = meta_get_all(sh)
        status = meta.get("status","pending").lower()
        ename = meta.get("name","(unnamed)")
        # Time window check
        now_cet = datetime.now(CET)
        start_str = meta.get("start_cet","")
        end_str = meta.get("end_cet","")
        def parse_dt(s):
            try:
                return datetime.fromisoformat(s)
            except:
                return None
        start_dt = parse_dt(start_str)
        end_dt = parse_dt(end_str)

        st.markdown(f"**Election:** `{ename}`  •  **Status:** `{status}`")
        if start_dt: st.caption(f"Starts: {start_dt} CET")
        if end_dt:   st.caption(f"Ends  : {end_dt} CET")

        # Gate by time/status
        allowed = False
        if status == "ongoing":
            allowed = True
        elif status in ("scheduled","pending"):
            if start_dt and start_dt <= now_cet and (not end_dt or now_cet <= end_dt):
                allowed = True
        elif status == "ended":
            allowed = False

        if not allowed:
            st.warning("⏳ এখন ভোটিং উইন্ডো খোলা নেই। সময়/স্ট্যাটাস চেক করুন।")
        else:
            # Token
            token = st.text_input("Enter your voting token")
            proceed = st.button("Proceed")
            if proceed:
                valid, df_index = validate_token(sh, token)
                if not valid:
                    st.error("❌ Invalid or already used token.")
                else:
                    # Load positions & candidates
                    pos_df = read_df(sh, "positions")
                    cand_df = read_df(sh, "candidates")
                    if pos_df.empty or cand_df.empty:
                        st.error("পদের তালিকা বা প্রার্থীদের তালিকা খালি। Admin ট্যাব থেকে যোগ করুন।")
                    else:
                        st.success("✅ Token OK. Please select your choices below.")
                        with st.form("full_ballot"):
                            selections = {}
                            # show all positions together
                            for _, row in pos_df.iterrows():
                                p = row["position"].strip()
                                cands = cand_df[cand_df["position"].str.strip() == p]["candidate"].tolist()
                                if not cands:
                                    st.warning(f"'{p}' পদের জন্য কোন প্রার্থী নেই।")
                                    continue
                                choice = st.radio(f"Position: {p}", options=cands, horizontal=True)
                                selections[p] = choice
                            submitted = st.form_submit_button("Submit All Votes")
                            if submitted:
                                # Write one row per position
                                ws_votes = sh.worksheet("votes")
                                timestamp_cet = datetime.now(CET).isoformat()
                                rows_to_add = []
                                for p, c in selections.items():
                                    rows_to_add.append([ename, p, c, token.strip(), timestamp_cet])
                                if rows_to_add:
                                    ws_votes.append_rows(rows_to_add)
                                    mark_token_used(sh, df_index)
                                    write_results_from_votes(sh)
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
        auto = st.toggle(f"Auto refresh every {LIVE_REFRESH_SEC}s", value=False,
                         help="Auto refresh চালু করলে API কোটায় চাপ পড়তে পারে।")
        if auto:
            st_autorefresh(interval=LIVE_REFRESH_SEC * 1000, key="auto_refresh")

        c1, _ = st.columns([1, 3])
        if c1.button("🔄 Refresh now"):
            st.rerun()

        # load from 'results' (already aggregated)
        res_df = read_df(sh, "results")
        if res_df.empty:
            st.info("এখনো কোনো ভোট পড়েনি বা ফলাফল তৈরি হয়নি। Admin ট্যাব থেকে Tally/Refresh দিন।")
        else:
            st.dataframe(res_df, use_container_width=True)

        st.divider()
        st.caption("টিপ: খুব ঘনঘন রিফ্রেশ করবেন না—429 quota exceeded এড়াতে।")

# ------------------------------------------------------------------------------
# ADMIN TAB
# ------------------------------------------------------------------------------
with tabs[2]:
    st.header("👨‍💻 Admin")
    if not api_ok:
        st.info("Sheets API ফিক্স করুন—Secrets/Permissions/Sheet ID চেক করুন।")
    else:
        meta = meta_get_all(sh)
        st.subheader("Election Setup (CET)")
        # Existing values
        ename = st.text_input("Election name", value=meta.get("name","BYWOB Election"))

        # Date & time pickers (separate inputs)
        now_cet = datetime.now(CET)
        default_start_date = date.fromisoformat(meta.get("start_date_cet", now_cet.date().isoformat()))
        default_end_date   = date.fromisoformat(meta.get("end_date_cet",   (now_cet + timedelta(days=1)).date().isoformat()))
        def parse_time(s, fallback):
            try:
                hh, mm = s.split(":")
                return time(int(hh), int(mm))
            except:
                return fallback

        default_start_time = parse_time(meta.get("start_time_cet", "09:00"), time(9,0))
        default_end_time   = parse_time(meta.get("end_time_cet",   "18:00"), time(18,0))

        c1, c2 = st.columns(2)
        start_date = c1.date_input("Start date (CET)", value=default_start_date)
        start_time = c1.time_input("Start time (CET)", value=default_start_time)  # step default (1 minute)
        end_date   = c2.date_input("End date (CET)",   value=default_end_date)
        end_time   = c2.time_input("End time (CET)",   value=default_end_time)

        # Compose full datetimes
        start_dt_cet = datetime.combine(start_date, start_time, tzinfo=CET)
        end_dt_cet   = datetime.combine(end_date,   end_time,   tzinfo=CET)

        st.write(f"**Start (CET):** {start_dt_cet}")
        st.write(f"**End (CET):** {end_dt_cet}")

        c3, c4, c5 = st.columns([1,1,2])
        if c3.button("💾 Save Config"):
            meta_bulk_set(sh, {
                "name": ename,
                "start_date_cet": start_date.isoformat(),
                "start_time_cet": start_time.strftime("%H:%M"),
                "end_date_cet": end_date.isoformat(),
                "end_time_cet": end_time.strftime("%H:%M"),
                "start_cet": start_dt_cet.isoformat(),
                "end_cet": end_dt_cet.isoformat(),
                "status": "scheduled"
            })
            st.success("Config saved (status=scheduled).")

        if c4.button("▶️ Start Election Now"):
            now_cet = datetime.now(CET)
            # keep end as set, but set start to now, status ongoing
            meta_bulk_set(sh, {
                "name": ename,
                "start_date_cet": now_cet.date().isoformat(),
                "start_time_cet": now_cet.strftime("%H:%M"),
                "start_cet": now_cet.isoformat(),
                "status": "ongoing"
            })
            st.success(f"Election started at {now_cet} CET (status=ongoing).")

        if c5.button("⏹ End Election Now"):
            now_cet = datetime.now(CET)
            meta_bulk_set(sh, {
                "end_date_cet": now_cet.date().isoformat(),
                "end_time_cet": now_cet.strftime("%H:%M"),
                "end_cet": now_cet.isoformat(),
                "status": "ended"
            })
            st.warning(f"Election ended at {now_cet} CET (status=ended).")

        st.divider()
        st.subheader("Positions & Candidates")
        # Show editors
        pos_df = read_df(sh, "positions")
        cand_df = read_df(sh, "candidates")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**positions**")
            st.dataframe(pos_df if not pos_df.empty else pd.DataFrame(columns=["position"]), use_container_width=True)
        with c2:
            st.markdown("**candidates**")
            st.dataframe(cand_df if not cand_df.empty else pd.DataFrame(columns=["position","candidate"]), use_container_width=True)

        with st.expander("➕ Quick add"):
            c1, c2, c3 = st.columns([2,2,1])
            new_pos = c1.text_input("Add position")
            if c3.button("Add position"):
                if new_pos.strip():
                    sh.worksheet("positions").append_row([new_pos.strip()])
                    st.success("Position added.")
                    st.rerun()
            new_pos2 = c1.selectbox("Position for candidate", options=(pos_df["position"].tolist() if not pos_df.empty else []))
            new_cand = c2.text_input("Candidate name")
            if c3.button("Add candidate"):
                if new_pos2 and new_cand.strip():
                    sh.worksheet("candidates").append_row([new_pos2, new_cand.strip()])
                    st.success("Candidate added.")
                    st.rerun()

        st.divider()
        st.subheader("Voters & Tokens")
        voters_df = read_df(sh, "voters")
        st.dataframe(voters_df if not voters_df.empty else pd.DataFrame(columns=["name","email","token","used","used_at"]),
                     use_container_width=True)

        with st.expander("🔑 Token Generator"):
            t1, t2, t3 = st.columns([1,1,1])
            count = t1.number_input("How many", min_value=1, max_value=2000, value=50, step=10)
            prefix = t2.text_input("Prefix", value="BYWOB-2025-")
            do_gen = t3.button("Generate & Append")
            if do_gen:
                toks = generate_tokens(count, prefix)
                rows = [[ "", "", tok, "FALSE", "" ] for tok in toks]
                sh.worksheet("voters").append_rows(rows)
                st.success(f"{len(toks)} tokens appended.")
                st.rerun()

        st.divider()
        st.subheader("Tally & Export")
        c1, c2, c3 = st.columns([1,1,2])
        if c1.button("🧮 Recompute tally"):
            write_results_from_votes(sh)
            st.success("Results recomputed from votes.")
        if c2.button("📤 Export results CSV"):
            res_df = read_df(sh, "results")
            if res_df.empty:
                st.info("Results empty.")
            else:
                csv = res_df.to_csv(index=False).encode("utf-8")
                st.download_button("Download results.csv", data=csv, file_name="results.csv", mime="text/csv")

        st.divider()
        st.subheader("Archive & Reset")
        a1, a2 = st.columns([1,2])
        if a1.button("📦 Archive previous votes & reset tokens"):
            # Archive votes -> new worksheet, clear votes, reset voters.used
            ts = datetime.now(CET).strftime("%Y%m%d_%H%M%S")
            votes_df = read_df(sh, "votes")
            archive_title = f"votes_archive_{ts}"
            if votes_df.empty:
                st.info("No votes to archive.")
            else:
                ws_arch = sh.add_worksheet(archive_title, rows=2, cols=max(5, votes_df.shape[1]))
                ws_arch.update("A1", [votes_df.columns.tolist()] + votes_df.values.tolist())
                # Clear votes
                sh.worksheet("votes").clear()
                sh.worksheet("votes").update("A1", [["election_name","position","candidate","token","timestamp_cet"]])
            # reset voters
            vws = sh.worksheet("voters")
            vals = vws.get_all_values()
            if vals:
                # overwrite used & used_at to FALSE/""
                header = vals[0]
                used_idx = header.index("used")
                used_at_idx = header.index("used_at")
                for r in range(1, len(vals)):
                    if len(vals[r]) < len(header):
                        vals[r] += [""]*(len(header)-len(vals[r]))
                    vals[r][used_idx] = "FALSE"
                    vals[r][used_at_idx] = ""
                vws.clear()
                vws.update("A1", vals)
            st.success("Archived old votes & reset tokens.")

        st.divider()
        with st.expander("🧰 Diagnostics"):
            st.write("**Service Account:**", st.secrets["gcp_service_account"]["client_email"])
            st.write("**Project ID:**", st.secrets["gcp_service_account"]["project_id"])
            try:
                # simple ping
                _ = sh.worksheets()
                st.success("Sheets API reachable ✅")
            except Exception as e:
                st.error(f"Sheets API problem: {e}")

        st.divider()
        with st.expander("📋 Operator Guide"):
            st.markdown("""
**দ্রুত গাইড (CET):**
1. `positions` ও `candidates` ট্যাব পূরণ করুন।  
2. Token Generator থেকে টোকেন বানান (voters শিটে যাবে)।  
3. Election name + Start/End (CET) সেট করে **Save Config** দিন।  
4. সময় হলে নিজে থেকেই ভোট খুলবে, না হলে **Start Election Now** চাপুন।  
5. ভোট চলাকালীন **Results** ট্যাবে **Refresh** বা **Auto refresh** ব্যবহার করুন।  
6. শেষ হলে **End Election Now** দিন (বা সময় শেষ হলে বন্ধ হবে)।  
7. ফলাফল **Export results CSV** দিয়ে নামিয়ে রাখুন।  
8. নতুন নির্বাচন শুরুর আগে **Archive previous votes & reset tokens** দিন।
            """)
