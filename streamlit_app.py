# streamlit_app.py
# BYWOB Online Voting — Streamlit + Google Sheets
# - Auto-creates worksheets (meta, positions, candidates, voters, votes, results)
# - One-time token voting, all positions shown together
# - CET scheduling + "Start Now" overwrites only start; "End Now" overwrites only end
# - Live tally, CSV export, archive + reset
# - Aggressive caching + gentle retry to reduce 429s

import random
import string
import time
from datetime import datetime, date, time as dtime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps

import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound

# ------------------------------------------------------------------------------
# App Config
# ------------------------------------------------------------------------------
st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="wide")
CET = ZoneInfo("Europe/Paris")
LIVE_REFRESH_SEC = 30            # keep >= 30 to be quota-safe
RETRY_MAX_TRIES = 3
RETRY_SLEEP_SEC = 2.0            # initial backoff seconds

def now_cet():
    return datetime.now(CET)

# ------------------------------------------------------------------------------
# Secrets & Google Sheets connection (cached)
# ------------------------------------------------------------------------------
def _require_secrets():
    if "gcp_service_account" not in st.secrets:
        st.error("Secrets missing: gcp_service_account. Add the service account JSON and SHEET_ID.")
        st.stop()

_require_secrets()

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

try:
    creds, client, sh, SHEET_ID = get_client_and_sheet()
    api_ok = True
except Exception as e:
    api_ok = False
    st.error(f"❌ Google Sheet ওপেন করা যায়নি: {type(e).__name__}: {e}")

# ------------------------------------------------------------------------------
# Gentle retry wrapper (helps transient 429s)
# ------------------------------------------------------------------------------
def gs_retry(fn, *args, **kwargs):
    tries = 0
    sleep = RETRY_SLEEP_SEC
    while True:
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            tries += 1
            msg = str(e).lower()
            if tries >= RETRY_MAX_TRIES:
                raise
            if "quota" in msg or "429" in msg or "rate" in msg:
                time.sleep(sleep)
                sleep *= 2
            else:
                time.sleep(sleep)
        except Exception:
            # non-APIError but transient
            tries += 1
            if tries >= RETRY_MAX_TRIES:
                raise
            time.sleep(sleep)
            sleep *= 2

# ------------------------------------------------------------------------------
# Ensure worksheets + headers (run once per deployment)
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
    ensure_worksheet(_sh, "meta", ["key", "value"], rows=50, cols=2)
    ensure_worksheet(_sh, "positions", ["position"], rows=500, cols=1)
    ensure_worksheet(_sh, "candidates", ["position", "candidate"], rows=2000, cols=2)
    ensure_worksheet(_sh, "voters", ["name", "email", "token", "used", "used_at"], rows=5000, cols=5)
    ensure_worksheet(_sh, "votes", ["election_name", "position", "candidate", "token", "timestamp_cet"], rows=50000, cols=5)
    ensure_worksheet(_sh, "results", ["position", "candidate", "votes"], rows=1000, cols=3)
    return True

if api_ok:
    try:
        _ = setup_structure_once(sh)
    except Exception as e:
        api_ok = False
        st.error(f"❌ Sheet setup failed: {e}")

# ------------------------------------------------------------------------------
# Meta helpers (cached)
# ------------------------------------------------------------------------------
@st.cache_data(ttl=30, show_spinner=False)
def meta_get_all_cached(sheet_id: str):
    ws = gs_retry(sh.worksheet, "meta")
    rows = gs_retry(ws.get_all_records)
    return {r["key"]: r["value"] for r in rows}

def meta_set(key: str, value: str):
    ws = gs_retry(sh.worksheet, "meta")
    rows = gs_retry(ws.get_all_records)
    d = {r["key"]: r["value"] for r in rows}
    d[key] = value
    gs_retry(ws.clear)
    gs_retry(ws.update, "A1", [["key","value"]] + [[k, d[k]] for k in d])
    meta_get_all_cached.clear()

def meta_bulk_set(kv: dict):
    ws = gs_retry(sh.worksheet, "meta")
    rows = gs_retry(ws.get_all_records)
    d = {r["key"]: r["value"] for r in rows}
    d.update(kv)
    gs_retry(ws.clear)
    gs_retry(ws.update, "A1", [["key","value"]] + [[k, d[k]] for k in d])
    meta_get_all_cached.clear()

# ------------------------------------------------------------------------------
# Cached table readers
# ------------------------------------------------------------------------------
@st.cache_data(ttl=30, show_spinner=False)
def read_df_cached(sheet_id: str, tab: str) -> pd.DataFrame:
    ws = gs_retry(sh.worksheet, tab)
    vals = gs_retry(ws.get_all_values)
    if not vals:
        return pd.DataFrame()
    return pd.DataFrame(vals[1:], columns=vals[0])

def read_df(tab: str) -> pd.DataFrame:
    return read_df_cached(SHEET_ID, tab)

def clear_all_data_caches():
    read_df_cached.clear()
    meta_get_all_cached.clear()

# ------------------------------------------------------------------------------
# Results recompute (only when needed)
# ------------------------------------------------------------------------------
def write_results_from_votes():
    votes = read_df("votes")
    if votes.empty:
        df = pd.DataFrame(columns=["position", "candidate", "votes"])
    else:
        grp = votes.groupby(["position","candidate"]).size().reset_index(name="votes")
        df = grp.sort_values(["position","votes"], ascending=[True, False])
    ws = gs_retry(sh.worksheet, "results")
    gs_retry(ws.clear)
    gs_retry(
        ws.update,
        "A1",
        ([df.columns.tolist()] + df.values.tolist())
        if not df.empty
        else [["position","candidate","votes"]],
    )
    read_df_cached.clear()

# ------------------------------------------------------------------------------
# Voting helpers
# ------------------------------------------------------------------------------
def validate_token(token_str: str):
    voters = read_df("voters")
    if voters.empty:
        return False, None
    t = token_str.strip()
    row = voters[voters["token"].astype(str).str.strip().str.upper() == t.upper()]
    if row.empty:
        return False, None
    used = str(row.iloc[0].get("used", "")).strip().lower()
    if used in ("true", "1", "yes", "y"):
        return False, None
    return True, row.index[0]  # df index (0-based excluding header)

def mark_token_used(df_index: int):
    ws = gs_retry(sh.worksheet, "voters")
    row_num = df_index + 2  # header at row 1
    used_col = 4
    used_at_col = 5
    gs_retry(ws.update_cell, row_num, used_col, "TRUE")
    gs_retry(ws.update_cell, row_num, used_at_col, now_cet().isoformat())
    read_df_cached.clear()

def generate_tokens(count: int, prefix: str):
    tokens = []
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(count):
        suffix = "".join(random.choices(alphabet, k=6))
        tokens.append(f"{prefix}{suffix}")
    rows = [["", "", tok, "FALSE", ""] for tok in tokens]
    ws = gs_retry(sh.worksheet, "voters")
    gs_retry(ws.append_rows, rows)
    read_df_cached.clear()
    return len(tokens)

# ------------------------------------------------------------------------------
# Voting window
# ------------------------------------------------------------------------------
def parse_iso(s: str):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def is_voting_open(meta: dict) -> bool:
    status = meta.get("status", "idle").lower()
    if status != "ongoing":
        return False
    start_dt = parse_iso(meta.get("start_cet", ""))
    end_dt   = parse_iso(meta.get("end_cet", ""))
    now_ = now_cet()
    if start_dt and now_ < start_dt.astimezone(CET):
        return False
    if end_dt and now_ > end_dt.astimezone(CET):
        # auto-close and persist
        meta_set("status", "ended")
        return False
    return True

# ------------------------------------------------------------------------------
# Header / Sidebar
# ------------------------------------------------------------------------------
st.title("🗳️ BYWOB Voting Platform")
with st.sidebar:
    st.caption(f"SA: {creds.service_account_email} | Project: {creds.project_id}") if api_ok else None
    st.markdown("**Sheet ID:**")
    st.code(st.secrets["gcp_service_account"]["SHEET_ID"] if api_ok else "(unavailable)")

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
        ename = meta.get("name", "(unnamed)")
        status = meta.get("status", "idle")
        sdt = meta.get("start_cet", "")
        edt = meta.get("end_cet", "")

        st.markdown(f"**Election:** `{ename}`  •  **Status:** `{status}`")
        if sdt: st.caption(f"Starts (CET): {sdt}")
        if edt: st.caption(f"Ends (CET): {edt}")

        # Token entry first (no sheet reads until needed)
        token = st.text_input("Enter your voting token")
        proceed = st.button("Proceed")

        # Preserve ballot state to avoid disappearing UI
        if "ballot" not in st.session_state:
            st.session_state.ballot = {"ready": False}

        if proceed:
            # Check window
            if not is_voting_open(meta_get_all_cached(SHEET_ID)):
                st.error("⏳ Voting window is not open right now.")
                st.stop()

            # Validate token (single read)
            valid, df_index = validate_token(token)
            if not valid:
                st.error("❌ Invalid or already used token.")
                st.stop()

            # Load positions & candidates once
            pos_df = read_df("positions")
            cand_df = read_df("candidates")
            if pos_df.empty or cand_df.empty:
                st.error("Positions or Candidates sheet is empty. Admin must fill them first.")
                st.stop()

            # Build mapping position -> candidates list
            pos_to_cands = {}
            for _, prow in pos_df.iterrows():
                p = (prow.get("position") or "").strip()
                if not p:
                    continue
                cands = cand_df[cand_df["position"].astype(str).str.strip() == p]["candidate"].astype(str).str.strip().tolist()
                if cands:
                    pos_to_cands[p] = cands

            if not pos_to_cands:
                st.error("No candidates found for the listed positions.")
                st.stop()

            # Store minimal state so the page doesn't rebuild from sheets on every widget interaction
            st.session_state.ballot = {
                "ready": True,
                "token": token.strip(),
                "df_index": df_index,
                "ename": ename,
                "pos_to_cands": pos_to_cands,
            }
            st.rerun()

        # Render ballot if prepared
        if st.session_state.ballot.get("ready"):
            st.success("✅ Token verified. Please cast your votes.")
            pos_to_cands = st.session_state.ballot["pos_to_cands"]

            with st.form("full_ballot"):
                selections = {}
                for pos, cands in pos_to_cands.items():
                    choice = st.radio(f"Position: {pos}", options=cands, horizontal=True, key=f"radio_{pos}")
                    selections[pos] = choice

                c1, c2 = st.columns(2)
                submitted = c1.form_submit_button("✅ Submit All Votes")
                canceled  = c2.form_submit_button("❌ Cancel")

            if canceled:
                st.session_state.ballot = {"ready": False}
                st.rerun()

            if submitted:
                # Validate window again quickly
                if not is_voting_open(meta_get_all_cached(SHEET_ID)):
                    st.error("Voting window just closed. Your vote was not recorded.")
                    st.session_state.ballot = {"ready": False}
                    st.rerun()

                token_clean = st.session_state.ballot["token"]
                df_index    = st.session_state.ballot["df_index"]
                ename       = st.session_state.ballot["ename"]

                # Write all votes in one append_rows
                ws_votes = gs_retry(sh.worksheet, "votes")
                timestamp_cet = now_cet().isoformat()
                rows_to_add = []
                for p, c in selections.items():
                    rows_to_add.append([ename, p, c, token_clean, timestamp_cet])

                if rows_to_add:
                    gs_retry(ws_votes.append_rows, rows_to_add)
                    mark_token_used(df_index)
                    write_results_from_votes()
                    st.success("🎉 Your vote has been recorded.")
                else:
                    st.error("No selections made.")

                st.session_state.ballot = {"ready": False}
                st.rerun()

# ------------------------------------------------------------------------------
# RESULTS TAB
# ------------------------------------------------------------------------------
with tabs[1]:
    st.header("📊 Results (Live)")
    if not api_ok:
        st.info("Sheets API ঠিক না হওয়া পর্যন্ত ফলাফল দেখা যাবে না।")
    else:
        c1, c2, _ = st.columns([1,1,3])
        auto = c1.toggle(f"Auto refresh ({LIVE_REFRESH_SEC}s)", value=False,
                         help="Use sparingly to avoid API quota.")
        if c2.button("🔄 Refresh now"):
            read_df_cached.clear()
            st.rerun()

        if auto:
            # lightweight meta-refresh
            st.markdown(f"<meta http-equiv='refresh' content='{LIVE_REFRESH_SEC}'>", unsafe_allow_html=True)

        res_df = read_df("results")
        if res_df.empty:
            st.info("No results yet. Cast votes or recompute from Admin.")
        else:
            st.dataframe(res_df, use_container_width=True)

        st.caption("Tip: Avoid very frequent refresh to prevent 429 quota exceeded.")

# ------------------------------------------------------------------------------
# ADMIN TAB
# ------------------------------------------------------------------------------
with tabs[2]:
    st.header("👨‍💻 Admin")
    if not api_ok:
        st.info("Fix Sheets API first—check Secrets/Permissions/Sheet ID.")
    else:
        meta = meta_get_all_cached(SHEET_ID)

        st.subheader("Election Setup (CET)")
        ename = st.text_input("Election name", value=meta.get("name","BYWOB Election"))

        # defaults
        now_ = now_cet()
        def dparse(s, fb):
            try: return date.fromisoformat(s)
            except: return fb
        def tparse(s, fb):
            try:
                hh, mm = s.split(":")
                return dtime(int(hh), int(mm))
            except:
                return fb

        default_start_dt = parse_iso(meta.get("start_cet","")) or now_
        default_end_dt   = parse_iso(meta.get("end_cet",""))   or (now_ + timedelta(days=1))

        c1, c2 = st.columns(2)
        start_date = c1.date_input("Start date (CET)", value=default_start_dt.date())
        start_time = c1.time_input("Start time (CET)", value=default_start_dt.time().replace(second=0, microsecond=0))
        end_date   = c2.date_input("End date (CET)",   value=default_end_dt.date())
        end_time   = c2.time_input("End time (CET)",   value=default_end_dt.time().replace(second=0, microsecond=0))

        start_dt_cet = datetime.combine(start_date, start_time, tzinfo=CET)
        end_dt_cet   = datetime.combine(end_date,   end_time,   tzinfo=CET)

        st.write(f"**Start (CET):** {start_dt_cet}")
        st.write(f"**End (CET):** {end_dt_cet}")

        c3, c4, c5 = st.columns([1,1,2])

        if c3.button("💾 Save Config"):
            meta_bulk_set({
                "name": ename,
                "start_cet": start_dt_cet.isoformat(),
                "end_cet": end_dt_cet.isoformat(),
                "status": "scheduled",     # scheduled until you start
                "published": meta.get("published","FALSE"),
            })
            st.success("Config saved (status=scheduled).")
            st.rerun()

        if c4.button("▶️ Start Election Now"):
            # Overwrite only the start time to now, keep the scheduled end
            now_start = now_cet()
            end_keep = parse_iso(meta_get_all_cached(SHEET_ID).get("end_cet","")) or end_dt_cet
            meta_bulk_set({
                "name": ename,
                "start_cet": now_start.isoformat(),
                "end_cet": end_keep.isoformat(),
                "status": "ongoing",
            })
            st.success(f"Election started at {now_start} CET (end stays {end_keep}).")
            st.rerun()

        if c5.button("⏹ End Election Now"):
            # Overwrite only the end time to now, don't change start
            now_end = now_cet()
            start_keep = parse_iso(meta_get_all_cached(SHEET_ID).get("start_cet","")) or start_dt_cet
            meta_bulk_set({
                "name": ename,
                "start_cet": start_keep.isoformat(),
                "end_cet": now_end.isoformat(),
                "status": "ended",
            })
            st.warning(f"Election ended at {now_end} CET.")
            st.rerun()

        st.divider()
        st.subheader("Positions & Candidates (read-only here—edit in Google Sheet)")
        pos_df = read_df("positions")
        cand_df = read_df("candidates")
        cpc1, cpc2 = st.columns(2)
        with cpc1:
            st.markdown("**positions**")
            st.dataframe(pos_df if not pos_df.empty else pd.DataFrame(columns=["position"]), use_container_width=True)
        with cpc2:
            st.markdown("**candidates**")
            st.dataframe(cand_df if not cand_df.empty else pd.DataFrame(columns=["position","candidate"]), use_container_width=True)

        st.divider()
        st.subheader("Voters (tokens hidden)")
        voters_df = read_df("voters")
        if voters_df.empty:
            st.info("No voters yet.")
        else:
            safe = voters_df.copy()
            if "token" in safe.columns:
                safe["token"] = "••••••••"
            st.dataframe(safe, use_container_width=True)

        st.divider()
        st.subheader("🔑 Token Generator")
        t1, t2, t3 = st.columns([1,1,2])
        count = t1.number_input("How many", min_value=1, max_value=5000, value=50, step=10)
        prefix = t2.text_input("Prefix", value="BYWOB-2025-")
        if t3.button("Generate & Append"):
            n = generate_tokens(int(count), prefix)
            st.success(f"{n} tokens appended to voters sheet.")
            st.rerun()

        st.divider()
        st.subheader("Tally & Export")
        cta1, cta2, _ = st.columns([1,1,2])
        if cta1.button("🧮 Recompute tally"):
            write_results_from_votes()
            st.success("Results recomputed from votes.")
            st.rerun()
        if cta2.button("📤 Export results CSV"):
            res_df = read_df("results")
            if res_df.empty:
                st.info("Results empty.")
            else:
                csv = res_df.to_csv(index=False).encode("utf-8")
                st.download_button("Download results.csv", data=csv, file_name="results.csv", mime="text/csv")

        st.divider()
        st.subheader("Archive & Reset")
        a1, _ = st.columns([1,2])
        if a1.button("📦 Archive previous votes & reset tokens"):
            # Archive votes -> new worksheet, clear votes, reset voters.used
            ts = now_cet().strftime("%Y%m%d_%H%M%S")
            votes_df = read_df("votes")
            if votes_df.empty:
                st.info("No votes to archive.")
            else:
                ws_arch = gs_retry(sh.add_worksheet, f"votes_archive_{ts}", rows=max(2, votes_df.shape[0]+2), cols=max(5, votes_df.shape[1]))
                gs_retry(ws_arch.update, "A1", [votes_df.columns.tolist()] + votes_df.values.tolist())
                ws_votes = gs_retry(sh.worksheet, "votes")
                gs_retry(ws_votes.clear)
                gs_retry(ws_votes.update, "A1", [["election_name","position","candidate","token","timestamp_cet"]])

            # reset voters used flags
            vws = gs_retry(sh.worksheet, "voters")
            vals = gs_retry(vws.get_all_values)
            if vals:
                header = vals[0]
                used_idx = header.index("used")
                used_at_idx = header.index("used_at")
                for r in range(1, len(vals)):
                    if len(vals[r]) < len(header):
                        vals[r] += [""]*(len(header)-len(vals[r]))
                    vals[r][used_idx] = "FALSE"
                    vals[r][used_at_idx] = ""
                gs_retry(vws.clear)
                gs_retry(vws.update, "A1", vals)

            clear_all_data_caches()
            st.success("Archived old votes & reset tokens.")
            st.rerun()

        st.divider()
        with st.expander("🧰 Diagnostics"):
            st.write("**Service Account:**", st.secrets["gcp_service_account"]["client_email"])
            st.write("**Project ID:**", st.secrets["gcp_service_account"]["project_id"])
            try:
                _ = gs_retry(sh.worksheets)
                st.success("Sheets API reachable ✅")
            except Exception as e:
                st.error(f"Sheets API problem: {e}")
