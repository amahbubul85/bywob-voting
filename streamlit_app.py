# streamlit_app.py
# BYWOB Online Voting — Streamlit + Google Sheets

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
# CET Timezone (fixed offset; simple & works on Streamlit Cloud)
# --------------------------------------------------------------------------------------
CET = timezone(timedelta(hours=1))  # CET ≈ UTC+1 (no DST handling here)

def now_cet():
    return datetime.now(CET)

def to_cet(dt: datetime):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=CET)
    return dt.astimezone(CET)

# --------------------------------------------------------------------------------------
# API rate limiter + retry
# --------------------------------------------------------------------------------------
def rate_limited(max_per_minute):
    min_interval = 60.0 / max_per_minute
    def decorator(func):
        last_called = [0.0]
        @wraps(func)
        def wrap(*args, **kwargs):
            elapsed = time.time() - last_called[0]
            wait = min_interval - elapsed
            if wait > 0:
                time.sleep(wait)
            out = func(*args, **kwargs)
            last_called[0] = time.time()
            return out
        return wrap
    return decorator

@rate_limited(60)
def rl(api_fn, *args, **kwargs):
    return api_fn(*args, **kwargs)

def with_retry(op, tries=3, backoff=2.0):
    err = None
    for i in range(tries):
        try:
            return rl(op)
        except Exception as e:
            msg = str(e).lower()
            err = e
            if ("429" in msg or "quota" in msg) and i < tries-1:
                time.sleep(backoff * (i+1))
                continue
            break
    raise err

# --------------------------------------------------------------------------------------
# Secrets & Sheets
# --------------------------------------------------------------------------------------
if "gcp_service_account" not in st.secrets:
    st.error("Secrets missing: gcp_service_account (must include JSON + SHEET_ID).")
    st.stop()

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
client = gspread.authorize(creds)

try:
    SHEET_ID = st.secrets["gcp_service_account"]["SHEET_ID"]
    sheet    = client.open_by_key(SHEET_ID)
except Exception as e:
    st.error(f"❌ Google Sheet ওপেন করা যায়নি: {e}")
    st.stop()

# --------------------------------------------------------------------------------------
# Worksheet helpers
# --------------------------------------------------------------------------------------
def ensure_ws(title: str, headers: list[str], rows=200, cols=10):
    try:
        ws = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        def _create(): return sheet.add_worksheet(title=title, rows=rows, cols=cols)
        ws = with_retry(_create)
        def _hdr(): ws.update(range_name=f"A1:{chr(64+len(headers))}1", values=[headers])
        with_retry(_hdr)
    return sheet.worksheet(title)

meta_ws       = ensure_ws("meta",       ["key","value"],          rows=20,   cols=2)
voters_ws     = ensure_ws("voters",     ["name","email","token","used","used_at"], rows=4000, cols=5)
candidates_ws = ensure_ws("candidates", ["position","candidate"], rows=1000, cols=2)
votes_ws      = ensure_ws("votes",      ["position","candidate","timestamp"], rows=10000, cols=3)

# --------------------------------------------------------------------------------------
# Meta helpers
# --------------------------------------------------------------------------------------
def meta_get_all() -> dict:
    recs = with_retry(meta_ws.get_all_records)
    return {r.get("key"): r.get("value") for r in recs if r.get("key")}

def meta_set(k: str, v: str):
    recs = with_retry(meta_ws.get_all_records)
    # update if exists
    for i, r in enumerate(recs, start=2):
        if r.get("key") == k:
            def _upd(): meta_ws.update_cell(i, 2, v)
            with_retry(_upd)
            return
    # append if new
    def _app(): meta_ws.append_row([k, v], value_input_option="RAW")
    with_retry(_app)

m0 = meta_get_all()
if "status"    not in m0: meta_set("status", "idle")       # idle | scheduled | ongoing | ended | published
if "name"      not in m0: meta_set("name",  "")
if "start_cet" not in m0: meta_set("start_cet", "")
if "end_cet"   not in m0: meta_set("end_cet", "")
if "published" not in m0: meta_set("published", "FALSE")

def is_voting_open() -> bool:
    m = meta_get_all()
    if m.get("status","idle") != "ongoing":
        return False
    try:
        start = m.get("start_cet","")
        end   = m.get("end_cet","")
        sdt = datetime.fromisoformat(start) if start else None
        edt = datetime.fromisoformat(end)   if end   else None
    except Exception:
        return False
    now = now_cet()
    if sdt and now < to_cet(sdt): return False
    if edt and now > to_cet(edt):
        meta_set("status","ended")
        return False
    return True

# --------------------------------------------------------------------------------------
# Cached loaders
# --------------------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def load_voters_df():
    df = pd.DataFrame(with_retry(voters_ws.get_all_records))
    if df.empty:
        df = pd.DataFrame(columns=["name","email","token","used","used_at"])
    for c in ["name","email","token","used","used_at"]:
        if c not in df.columns: df[c] = ""
    df["token"]     = df["token"].astype(str).str.strip()
    df["used_bool"] = df["used"].astype(str).str.strip().str.lower().isin(["true","1","yes"])
    return df[["name","email","token","used","used_at","used_bool"]]

@st.cache_data(ttl=300, show_spinner=False)
def load_candidates_df():
    df = pd.DataFrame(with_retry(candidates_ws.get_all_records))
    if df.empty:
        df = pd.DataFrame(columns=["position","candidate"])
    for c in ["position","candidate"]:
        if c not in df.columns: df[c] = ""
    df["position"]  = df["position"].astype(str).str.strip()
    df["candidate"] = df["candidate"].astype(str).str.strip()
    return df[(df["position"]!="") & (df["candidate"]!="")][["position","candidate"]]

@st.cache_data(ttl=300, show_spinner=False)
def load_votes_df():
    df = pd.DataFrame(with_retry(votes_ws.get_all_records))
    if df.empty:
        df = pd.DataFrame(columns=["position","candidate","timestamp"])
    return df[["position","candidate","timestamp"]]

def clear_caches():
    load_voters_df.clear(); load_candidates_df.clear(); load_votes_df.clear()

# --------------------------------------------------------------------------------------
# Sheet ops
# --------------------------------------------------------------------------------------
def mark_token_used(voters_df: pd.DataFrame, token: str):
    t = str(token).strip()
    m = voters_df[voters_df["token"] == t]
    if m.empty: return
    row = m.index[0] + 2
    def _op():
        voters_ws.update_cell(row, 4, "TRUE")
        voters_ws.update_cell(row, 5, now_cet().isoformat())
    with_retry(_op)
    load_voters_df.clear()

def append_vote(position: str, candidate: str):
    def _app(): votes_ws.append_row([position, candidate, now_cet().isoformat()], value_input_option="RAW")
    with_retry(_app)
    load_votes_df.clear()

def generate_tokens(n: int, prefix: str):
    import secrets, string
    alpha = string.ascii_uppercase + string.digits
    rows = []
    for _ in range(int(n)):
        tok = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        rows.append(["","",""+tok,"FALSE",""])
    if rows:
        def _app(): voters_ws.append_rows(rows, value_input_option="RAW")
        with_retry(_app)
        load_voters_df.clear()

def archive_and_clear_votes(election_name: str | None):
    rows = with_retry(votes_ws.get_all_records)
    if not rows: return "no_votes"
    ts   = now_cet().strftime("%Y%m%dT%H%M%S")
    safe = (election_name or "election").replace(" ","_")[:20]
    title = f"votes_archive_{safe}_{ts}"
    def _arch():
        new = sheet.add_worksheet(title=title, rows=len(rows)+5, cols=3)
        new.update("A1:C1", [["position","candidate","timestamp"]])
        new.append_rows([[r["position"], r["candidate"], r["timestamp"]] for r in rows], value_input_option="RAW")
        votes_ws.clear()
        votes_ws.append_row(["position","candidate","timestamp"], value_input_option="RAW")
        return title
    out = with_retry(_arch)
    load_votes_df.clear()
    return out

def results_df():
    df = load_votes_df()
    if df.empty: return pd.DataFrame(columns=["position","candidate","votes"])
    g = df.groupby(["position","candidate"]).size().reset_index(name="votes")
    return g.sort_values(["position","votes"], ascending=[True, False])

# --------------------------------------------------------------------------------------
# UI Tabs
# --------------------------------------------------------------------------------------
tab_vote, tab_results, tab_admin = st.tabs(["🗳️ Vote", "📊 Results", "🔑 Admin"])

# ------------------------ Vote Tab ------------------------
with tab_vote:
    # keep ballot state in session
    if "ballot" not in st.session_state:
        st.session_state.ballot = {"ready": False}

    # only auto-refresh when *not* in the ballot
    if is_voting_open() and not st.session_state.ballot["ready"]:
        st.markdown('<meta http-equiv="refresh" content="30">', unsafe_allow_html=True)

    st.subheader("ভোট দিন (টোকেন ব্যবহার করে)")

    # ---------- Stage 1: ask for token ----------
    if not st.session_state.ballot["ready"]:
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

            # snapshot candidates from cache so we don't re-read on every rerun
            cands = load_candidates_df()
            if cands.empty:
                st.warning("candidates শিট ফাঁকা। Admin ট্যাব থেকে প্রার্থী যোগ করুন।")
                st.stop()

            pos_to_cands = {
                p: cands.loc[cands["position"] == p, "candidate"].tolist()
                for p in cands["position"].unique().tolist()
            }

            st.session_state.ballot = {
                "ready": True,
                "token": token.strip(),
                "voters_snapshot": voters,      # to mark used without reloading
                "pos_to_cands": pos_to_cands,
            }
            st.experimental_rerun()

    # ---------- Stage 2: full ballot in one form ----------
    else:
        st.success("✅ Token OK. Select your choices below.")

        pos_to_cands = st.session_state.ballot["pos_to_cands"]

        with st.form("full_ballot"):
            # render all positions in one go; radios keep their own keys
            for position, options in pos_to_cands.items():
                st.markdown(f"#### {position}")
                # key must be stable & unique
                st.radio(
                    f"{position} এর জন্য প্রার্থী নির্বাচন করুন:",
                    options,
                    key=f"choice_{position}",
                    index=None,
                    horizontal=True,
                )

            submitted = st.form_submit_button("✅ Submit All Votes")

        # separate row of actions
        c1, c2 = st.columns([1, 1])
        if c2.button("❌ Cancel"):
            st.session_state.ballot = {"ready": False}
            st.experimental_rerun()

        if submitted:
            # build selections from session_state keys
            selections = {
                p: st.session_state.get(f"choice_{p}", None) for p in pos_to_cands.keys()
            }
            missing = [p for p, v in selections.items() if v is None]
            if missing:
                st.error("দয়া করে ভোট দিন: " + ", ".join(missing))
            else:
                # write all votes once; then mark token used
                for p, c in selections.items():
                    append_vote(p, c)
                mark_token_used(st.session_state.ballot["voters_snapshot"],
                                st.session_state.ballot["token"])
                st.success("আপনার সকল ভোট গ্রহণ করা হয়েছে। ধন্যবাদ!")
                # clear ballot state so the page resets cleanly
                st.session_state.ballot = {"ready": False}
                st.experimental_rerun()
# ---------------------- end vote tab ----------------------


# ------------------------ Results Tab ------------------------
with tab_results:
    st.subheader("📊 Live Results")
    r = results_df()
    if r.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
    else:
        st.dataframe(r, use_container_width=True)

# ------------------------ Admin Tab ------------------------
with tab_admin:
    st.subheader("🛠️ Admin Tools")

    admin_ok = True
    admin_pwd = st.secrets.get("ADMIN_PASSWORD")
    if admin_pwd:
        pwd = st.text_input("Admin password", type="password")
        admin_ok = (pwd == admin_pwd)
        if pwd and not admin_ok:
            st.error("Wrong password")

    if not admin_ok:
        st.warning("Please enter admin password to continue.")
        st.stop()

    m = meta_get_all()
    st.markdown("### 🗓️ Election control")
    st.markdown(f"- **Current election name:** `{m.get('name','(none)')}`")
    st.markdown(f"- **Status:** `{m.get('status','idle')}`")
    st.markdown(f"- **Start (CET):** `{m.get('start_cet','')}`")
    st.markdown(f"- **End (CET):** `{m.get('end_cet','')}`")
    st.markdown(f"- **Published:** `{m.get('published','FALSE')}`")

    st.divider()
    st.markdown("#### Create / Schedule new election")

    ename = st.text_input("Election name", value=m.get("name",""))

    # Pre-fill date/time from meta if present; else now
    def _parse_iso(s: str):
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    sdt_meta = _parse_iso(m.get("start_cet","")) or now_cet()
    edt_meta = _parse_iso(m.get("end_cet",""))   or (now_cet() + timedelta(hours=2))

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Start Time (CET)**")
        start_date = st.date_input("Start date", value=to_cet(sdt_meta).date(), key="start_date")
        start_time = st.time_input("Start time", value=to_cet(sdt_meta).time().replace(second=0, microsecond=0), key="start_time")
    with col2:
        st.markdown("**End Time (CET)**")
        end_date   = st.date_input("End date", value=to_cet(edt_meta).date(), key="end_date")
        end_time   = st.time_input("End time", value=to_cet(edt_meta).time().replace(second=0, microsecond=0), key="end_time")

    start_dt_cet = datetime.combine(start_date, start_time).replace(tzinfo=CET)
    end_dt_cet   = datetime.combine(end_date,   end_time).replace(tzinfo=CET)

    st.info(f"**সময়সূচী (CET):**\n- শুরু: {start_dt_cet.strftime('%Y-%m-%d %H:%M')}\n- শেষ: {end_dt_cet.strftime('%Y-%m-%d %H:%M')}")

    if st.button("Set & Schedule"):
        meta_set("name", ename)
        meta_set("start_cet", start_dt_cet.isoformat())
        meta_set("end_cet",   end_dt_cet.isoformat())
        meta_set("status",    "scheduled")
        meta_set("published", "FALSE")
        st.success("Election scheduled (status = scheduled).")
        st.rerun()

    c3, c4, c5 = st.columns(3)

    if c3.button("Start Election Now"):
        now = now_cet()
        # Overwrite start to NOW, keep the end from the inputs
        meta_set("name", ename)
        meta_set("start_cet", now.isoformat())
        meta_set("end_cet",   end_dt_cet.isoformat())
        meta_set("status",    "ongoing")
        st.success(f"Election started now ({now.strftime('%Y-%m-%d %H:%M CET')}). End stays at {end_dt_cet.strftime('%Y-%m-%d %H:%M CET')}.")
        st.rerun()

    if c4.button("End Election Now"):
        now = now_cet()
        # Overwrite end to NOW
        meta_set("end_cet", now.isoformat())
        meta_set("status",  "ended")
        st.success(f"Election ended now ({now.strftime('%Y-%m-%d %H:%M CET')}).")
        st.rerun()

    if c5.button("Publish Results (declare)"):
        meta_set("published", "TRUE")
        meta_set("status",    "ended")
        st.success("Results published.")
        st.rerun()

    st.divider()
    st.markdown("### 🔑 Token Generator")
    g1, g2 = st.columns(2)
    count  = g1.number_input("কতটি টোকেন?", min_value=1, value=20, step=10)
    prefix = g2.text_input("Prefix", value="BYWOB-2025")
    if st.button("➕ Generate & Append"):
        try:
            generate_tokens(int(count), prefix)
            st.success(f"{int(count)}টি টোকেন voters শিটে যোগ হয়েছে।")
            st.rerun()
        except Exception as e:
            st.error(f"টোকেন তৈরি ব্যর্থ: {e}")

    st.markdown("### 📋 Candidates")
    cands_df = load_candidates_df()
    if cands_df.empty:
        st.info("candidates শিট ফাঁকা। position, candidate কলামসহ ডেটা দিন।")
    else:
        st.dataframe(cands_df, use_container_width=True)

    st.markdown("### 👥 Voters (tokens hidden)")
    voters_df = load_voters_df()
    if voters_df.empty:
        st.info("কোনো ভোটার নেই।")
    else:
        safe = voters_df.copy()
        safe["token"] = "••••••••"
        st.dataframe(safe[["name","email","token","used","used_at"]], use_container_width=True)

    st.markdown("### 📈 Tally (by position)")
    vdf = load_votes_df()
    if vdf.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
    else:
        for pos in cands_df["position"].unique():
            grp = (
                vdf[vdf["position"] == pos]
                .groupby("candidate").size().reset_index(name="votes")
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