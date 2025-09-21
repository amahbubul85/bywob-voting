# BYWOB Online Voting — Streamlit + Google Sheets
# - One-screen voting (all positions together)
# - Auto-create worksheets (meta, voters, candidates, votes)
# - Election window (UTC): idle | ongoing | ended | published
# - Archive old votes and start a fresh election (reset tokens)
# - Token generator, live tally, CSV export

import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone, timedelta

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="centered")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + Google Sheets • Secret ballot with one-time tokens")

# --------------------------- Secrets & Auth ---------------------------
def _require_secrets():
    if "gcp_service_account" not in st.secrets:
        st.error("Secrets missing: gcp_service_account. App → Settings → Secrets এ service account JSON + SHEET_ID দিন।")
        st.stop()
_require_secrets()

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds  = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
client = gspread.authorize(creds)

try:
    SHEET_ID = st.secrets["gcp_service_account"]["SHEET_ID"]
    sheet = client.open_by_key(SHEET_ID)
except Exception as e:
    st.error(f"❌ Google Sheet ওপেন করা যায়নি: {e}")
    st.stop()

# --------------------------- Worksheet helpers ---------------------------
def ensure_ws(title: str, headers: list[str], rows: int = 100, cols: int = 10):
    try:
        ws = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=title, rows=rows, cols=cols)
        rng = f"A1:{chr(64+len(headers))}1"
        ws.update(values=[headers], range_name=rng)
    return sheet.worksheet(title)

meta_ws       = ensure_ws("meta", ["key", "value"], rows=20, cols=2)
voters_ws     = ensure_ws("voters", ["name", "email", "token", "used", "used_at"], rows=2000, cols=5)
candidates_ws = ensure_ws("candidates", ["position", "candidate"], rows=500, cols=2)
votes_ws      = ensure_ws("votes", ["position", "candidate", "timestamp"], rows=5000, cols=3)

# --------------------------- Meta helpers ---------------------------
def now_utc(): return datetime.now(timezone.utc)

def meta_get_all() -> dict:
    recs = meta_ws.get_all_records()
    return {r.get("key"): r.get("value") for r in recs if r.get("key")}

def meta_set(key: str, value: str):
    recs = meta_ws.get_all_records()
    for i, r in enumerate(recs, start=2):
        if r.get("key") == key:
            meta_ws.update_cell(i, 2, value); return
    meta_ws.append_row([key, value], value_input_option="RAW")

# defaults (first run)
_m = meta_get_all()
if "status" not in _m:     meta_set("status", "idle")
if "name" not in _m:       meta_set("name", "")
if "start_utc" not in _m:  meta_set("start_utc", "")
if "end_utc" not in _m:    meta_set("end_utc", "")
if "published" not in _m:  meta_set("published", "FALSE")

def is_voting_open() -> bool:
    m = meta_get_all()
    if m.get("status", "idle") != "ongoing":
        return False
    try:
        s = m.get("start_utc", "")
        e = m.get("end_utc", "")
        start_dt = datetime.fromisoformat(s) if s else None
        end_dt   = datetime.fromisoformat(e) if e else None
    except Exception:
        return False
    now = now_utc()
    if start_dt and now < start_dt.astimezone(timezone.utc): return False
    if end_dt and now > end_dt.astimezone(timezone.utc):
        meta_set("status", "ended")
        return False
    return True

# --------------------------- Cached data loaders ---------------------------
@st.cache_data(show_spinner=False)
def load_voters_df():
    df = pd.DataFrame(voters_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["name","email","token","used","used_at"])
    for c in ["name","email","token","used","used_at"]:
        if c not in df.columns: df[c] = ""
    df["token"] = df["token"].astype(str).str.strip()
    df["used_bool"] = df["used"].astype(str).str.strip().str.lower().isin(["true","1","yes"])
    return df[["name","email","token","used","used_at","used_bool"]]

@st.cache_data(show_spinner=False)
def load_candidates_df():
    df = pd.DataFrame(candidates_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["position","candidate"])
    for c in ["position","candidate"]:
        if c not in df.columns: df[c] = ""
    df["position"]  = df["position"].astype(str).str.strip()
    df["candidate"] = df["candidate"].astype(str).str.strip()
    df = df[(df["position"]!="") & (df["candidate"]!="")]
    return df[["position","candidate"]]

@st.cache_data(show_spinner=False)
def load_votes_df():
    df = pd.DataFrame(votes_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["position","candidate","timestamp"])
    return df[["position","candidate","timestamp"]]

def clear_caches():
    load_voters_df.clear(); load_candidates_df.clear(); load_votes_df.clear()

# --------------------------- Sheet operations ---------------------------
def mark_token_used(voters_df: pd.DataFrame, token: str):
    t = str(token).strip()
    m = voters_df[voters_df["token"] == t]
    if m.empty: return
    row_idx = m.index[0] + 2
    voters_ws.update_cell(row_idx, 4, "TRUE")
    voters_ws.update_cell(row_idx, 5, now_utc().isoformat())
    load_voters_df.clear()

def append_vote_rows(rows: list[list[str]]):
    # rows: [[position, candidate, timestamp], ...]
    if rows:
        votes_ws.append_rows(rows, value_input_option="RAW")
        load_votes_df.clear()

def generate_tokens(n: int, prefix: str):
    import secrets, string
    alpha = string.ascii_uppercase + string.digits
    rows = []
    for _ in range(n):
        tok = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        rows.append(["","",tok,"FALSE",""])
    if rows:
        voters_ws.append_rows(rows, value_input_option="RAW")
        load_voters_df.clear()

def archive_votes(election_name: str | None):
    rows = votes_ws.get_all_records()
    if not rows: return None
    ts = now_utc().strftime("%Y%m%dT%H%M%SZ")
    safe = (election_name or "election").replace(" ", "_")[:20]
    title = f"votes_archive_{safe}_{ts}"
    new_ws = sheet.add_worksheet(title=title, rows=len(rows)+5, cols=3)
    new_ws.update(values=[["position","candidate","timestamp"]], range_name="A1:C1")
    new_ws.append_rows([[r["position"],r["candidate"],r["timestamp"]] for r in rows], value_input_option="RAW")
    return title

def clear_votes_sheet():
    votes_ws.clear()
    votes_ws.append_row(["position","candidate","timestamp"], value_input_option="RAW")
    load_votes_df.clear()

def reset_all_tokens():
    df = load_voters_df()
    if df.empty: return 0
    # Overwrite used = FALSE, used_at = ""
    for i in range(len(df)):
        row_idx = i + 2
        voters_ws.update_cell(row_idx, 4, "FALSE")
        voters_ws.update_cell(row_idx, 5, "")
    load_voters_df.clear()
    return len(df)

def results_df():
    df = load_votes_df()
    if df.empty:
        return pd.DataFrame(columns=["position","candidate","votes"])
    out = df.groupby(["position","candidate"]).size().reset_index(name="votes")
    return out.sort_values(["position","votes"], ascending=[True, False])

# --------------------------- UI Tabs ---------------------------
tab_vote, tab_results, tab_admin = st.tabs(["🗳️ Vote", "📊 Results", "🔑 Admin"])

# --------------------------- Vote: all positions together ---------------------------
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
                f"Start (UTC): {m.get('start_utc','')}\n"
                f"End (UTC): {m.get('end_utc','')}"
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
            ts = now_utc().isoformat()
            for p, cand in selections.items():
                if cand and cand.strip():
                    rows.append([p, cand, ts])

            if not rows:
                st.error("কমপক্ষে একটি পদের জন্য প্রার্থী সিলেক্ট করুন।"); st.stop()

            append_vote_rows(rows)
            mark_token_used(voters, token)
            st.success("আপনার সব ভোট গ্রহণ করা হয়েছে। ধন্যবাদ!")

# --------------------------- Results ---------------------------
with tab_results:
    st.subheader("📊 Live Results")
    r = results_df()
    if r.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
    else:
        st.dataframe(r, width="stretch")

# --------------------------- Admin ---------------------------
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
    else:
        m = meta_get_all()
        st.markdown("### 🗓️ Election control")
        st.markdown(f"- **Current election name:** `{m.get('name','(none)')}`")
        st.markdown(f"- **Status:** `{m.get('status','idle')}`")
        st.markdown(f"- **Start (UTC):** `{m.get('start_utc','')}`")
        st.markdown(f"- **End (UTC):** `{m.get('end_utc','')}`")
        st.markdown(f"- **Published:** `{m.get('published','FALSE')}`")

        st.divider()
        st.markdown("#### Create / Schedule new election")

        now_ = now_utc()
        c1, c2 = st.columns(2)
        start_date = c1.date_input("Start date (UTC)", value=now_.date())
        end_date   = c2.date_input("End date (UTC)",   value=now_.date())

        mode = st.radio("Time input mode",
                        ["Picker (recommended)", "Manual (type HH:MM or HH:MM:SS)"],
                        horizontal=True)

        if mode == "Picker (recommended)":
            start_time = c1.time_input("Start time (UTC)", value=now_.time().replace(microsecond=0),
                                       step=timedelta(seconds=1))
            end_time   = c2.time_input("End time (UTC)",   value=now_.time().replace(microsecond=0),
                                       step=timedelta(seconds=1))
        else:
            st.caption("Tip: 24h format, যেমন 09:05 বা 09:05:30")
            cc1, cc2 = st.columns(2)
            s_str = cc1.text_input("Start time (UTC) — manual", value=now_.strftime("%H:%M:%S"))
            e_str = cc2.text_input("End time (UTC) — manual",   value=now_.strftime("%H:%M:%S"))
            def _p(s):
                for fmt in ("%H:%M:%S", "%H:%M"):
                    try: return datetime.strptime(s.strip(), fmt).time()
                    except ValueError: pass
                raise ValueError("Invalid time format. Use HH:MM or HH:MM:SS")
            try:
                start_time = _p(s_str); end_time = _p(e_str)
            except ValueError as e:
                st.error(str(e)); st.stop()

        start_dt = datetime.combine(start_date, start_time).replace(tzinfo=timezone.utc)
        end_dt   = datetime.combine(end_date,   end_time).replace(tzinfo=timezone.utc)

        ename = st.text_input("Election name", value=m.get("name",""))

        if st.button("Set & Schedule"):
            meta_set("name", ename)
            meta_set("start_utc", start_dt.isoformat())
            meta_set("end_utc", end_dt.isoformat())
            meta_set("status", "idle")
            meta_set("published", "FALSE")
            st.success(f"Election scheduled.\nStart: {start_dt.isoformat()}\nEnd: {end_dt.isoformat()}")

        c3, c4, c5 = st.columns(3)
        if c3.button("Start Election Now"):
            meta_set("status", "ongoing"); st.success("Election started.")
        if c4.button("End Election Now"):
            meta_set("status", "ended"); st.success("Election ended.")
        if c5.button("Publish Results (declare)"):
            meta_set("published", "TRUE"); meta_set("status", "ended"); st.success("Results published.")

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
