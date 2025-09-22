# streamlit_app.py
# BYWOB Online Voting — Streamlit + SQLite (no Google Sheets quota issues)

import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime, timezone, timedelta
import secrets, string

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="centered")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + SQLite • Secret ballot with one-time tokens")

# --------------------------------------------------------------------------------------
# CET Timezone
# --------------------------------------------------------------------------------------
CET = timezone(timedelta(hours=1))  # CET ≈ UTC+1 (no DST handling here)

def now_cet():
    return datetime.now(CET).replace(microsecond=0)

def to_cet(dt: datetime):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=CET)
    return dt.astimezone(CET)

# --------------------------------------------------------------------------------------
# SQLite setup
# --------------------------------------------------------------------------------------
conn = sqlite3.connect("election.db", check_same_thread=False)
cur = conn.cursor()

# Create tables
cur.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS voters (id INTEGER PRIMARY KEY, name TEXT, email TEXT, token TEXT UNIQUE, used INTEGER DEFAULT 0, used_at TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY, position TEXT, candidate TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS votes (id INTEGER PRIMARY KEY, position TEXT, candidate TEXT, timestamp TEXT)")
conn.commit()

# --------------------------------------------------------------------------------------
# Meta helpers
# --------------------------------------------------------------------------------------
def meta_get_all() -> dict:
    return dict(cur.execute("SELECT key,value FROM meta").fetchall())

def meta_set(k: str, v: str):
    cur.execute("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (k,v))
    conn.commit()

m0 = meta_get_all()
defaults = {
    "status":"idle",  # idle | scheduled | ongoing | ended | published
    "name":"", "start_cet":"", "end_cet":"", "published":"FALSE"
}
for k,v in defaults.items():
    if k not in m0: meta_set(k,v)

def is_voting_open() -> bool:
    m = meta_get_all()
    if m.get("status","idle") != "ongoing": return False
    try:
        sdt = datetime.fromisoformat(m.get("start_cet","")) if m.get("start_cet") else None
        edt = datetime.fromisoformat(m.get("end_cet","")) if m.get("end_cet") else None
    except Exception: return False
    now = now_cet()
    if sdt and now < to_cet(sdt): return False
    if edt and now > to_cet(edt):
        meta_set("status","ended")
        return False
    return True

# --------------------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------------------
def load_voters_df():
    df = pd.read_sql("SELECT * FROM voters", conn)
    if df.empty: return pd.DataFrame(columns=["id","name","email","token","used","used_at","used_bool"])
    df["used_bool"] = df["used"]==1
    return df

def load_candidates_df():
    return pd.read_sql("SELECT * FROM candidates", conn)

def load_votes_df():
    return pd.read_sql("SELECT * FROM votes", conn)

# --------------------------------------------------------------------------------------
# Ops
# --------------------------------------------------------------------------------------
def mark_token_used(token: str):
    cur.execute("UPDATE voters SET used=1, used_at=? WHERE token=?",(now_cet().isoformat(), token))
    conn.commit()

def append_vote(position: str, candidate: str):
    cur.execute("INSERT INTO votes (position,candidate,timestamp) VALUES (?,?,?)",(position,candidate,now_cet().isoformat()))
    conn.commit()

def generate_tokens(n: int, prefix: str):
    alpha = string.ascii_uppercase + string.digits
    for _ in range(int(n)):
        tok = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        cur.execute("INSERT OR IGNORE INTO voters (name,email,token,used,used_at) VALUES (?,?,?,?,?)", ("","",tok,0,""))
    conn.commit()

def results_df():
    df = load_votes_df()
    if df.empty: return pd.DataFrame(columns=["position","candidate","votes"])
    g = df.groupby(["position","candidate"]).size().reset_index(name="votes")
    return g.sort_values(["position","votes"], ascending=[True,False])

# --------------------------------------------------------------------------------------
# UI Tabs
# --------------------------------------------------------------------------------------
tab_vote, tab_results, tab_admin = st.tabs(["🗳️ Vote", "📊 Results", "🔑 Admin"])

# ------------------------ Vote Tab ------------------------
with tab_vote:
    if "ballot" not in st.session_state:
        st.session_state.ballot = {"ready": False}

    if is_voting_open() and not st.session_state.ballot["ready"]:
        st.markdown('<meta http-equiv="refresh" content="30">', unsafe_allow_html=True)

    st.subheader("ভোট দিন (টোকেন ব্যবহার করে)")

    if not st.session_state.ballot["ready"]:
        token = st.text_input("আপনার টোকেন লিখুন", placeholder="BYWOB-2025-XXXXXX")
        if st.button("Proceed"):
            if not token: st.error("টোকেন দিন।"); st.stop()
            if not is_voting_open():
                m = meta_get_all()
                st.error(f"এখন ভোট গ্রহণ করা হচ্ছে না। Status: {m.get('status')}")
                st.stop()
            voters = load_voters_df()
            row = voters[voters["token"] == token.strip()]
            if row.empty: st.error("❌ টোকেন সঠিক নয়।"); st.stop()
            if row["used_bool"].iloc[0]: st.error("⚠️ এই টোকেনটি ইতিমধ্যে ব্যবহার করা হয়েছে।"); st.stop()
            cands = load_candidates_df()
            if cands.empty: st.warning("No candidates set"); st.stop()
            pos_to_cands = {p: cands[cands["position"]==p]["candidate"].tolist() for p in cands["position"].unique()}
            st.session_state.ballot = {"ready":True, "token":token.strip(), "pos_to_cands":pos_to_cands}
            st.rerun()
    else:
        st.success("✅ Token OK. Select your choices below.")
        pos_to_cands = st.session_state.ballot["pos_to_cands"]
        with st.form("full_ballot"):
            for position, options in pos_to_cands.items():
                st.markdown(f"#### {position}")
                st.radio(f"{position} এর জন্য প্রার্থী নির্বাচন করুন:", options, key=f"choice_{position}", index=None, horizontal=True)
            submitted = st.form_submit_button("✅ Submit All Votes")
        if submitted:
            selections = {p: st.session_state.get(f"choice_{p}") for p in pos_to_cands}
            if None in selections.values(): st.error("সবগুলো পজিশনে ভোট দিন।")
            else:
                for p,c in selections.items(): append_vote(p,c)
                mark_token_used(st.session_state.ballot["token"])
                st.success("আপনার সকল ভোট গ্রহণ করা হয়েছে।")
                st.session_state.ballot = {"ready": False}
                st.rerun()

# ------------------------ Results Tab ------------------------
with tab_results:
    st.subheader("📊 Live Results")
    r = results_df()
    st.dataframe(r if not r.empty else pd.DataFrame([{"info":"No votes yet"}]), use_container_width=True)

# ------------------------ Admin Tab ------------------------
with tab_admin:
    st.subheader("🛠️ Admin Tools")
    m = meta_get_all()
    st.markdown(f"**Status:** {m.get('status')}  |  Start: {m.get('start_cet')}  |  End: {m.get('end_cet')}")

    # Schedule
    ename = st.text_input("Election name", value=m.get("name",""))
    sdt = st.date_input("Start date", value=now_cet().date())
    stm = st.time_input("Start time", value=now_cet().time().replace(second=0,microsecond=0))
    edt = st.date_input("End date", value=(now_cet()+timedelta(hours=2)).date())
    etm = st.time_input("End time", value=(now_cet()+timedelta(hours=2)).time().replace(second=0,microsecond=0))
    if st.button("Set & Schedule"):
        start_dt = datetime.combine(sdt,stm).replace(tzinfo=CET)
        end_dt = datetime.combine(edt,etm).replace(tzinfo=CET)
        meta_set("name", ename); meta_set("start_cet", start_dt.isoformat()); meta_set("end_cet", end_dt.isoformat()); meta_set("status","scheduled")
        st.success("Election scheduled.")

    c1,c2,c3 = st.columns(3)
    if c1.button("Start Now"):
        meta_set("start_cet", now_cet().isoformat()); meta_set("status","ongoing")
        st.success("Election started now")
    if c2.button("End Now"):
        meta_set("end_cet", now_cet().isoformat()); meta_set("status","ended")
        st.success("Election ended")
    if c3.button("Publish Results"):
        meta_set("published","TRUE"); meta_set("status","ended")
        st.success("Results published")

    # Token generator
    st.markdown("### 🔑 Generate tokens")
    num = st.number_input("কতটি টোকেন?", min_value=1, value=10)
    pref = st.text_input("Prefix", value="BYWOB-2025")
    if st.button("Generate"):
        generate_tokens(num, pref)
        st.success("Tokens generated")

    st.markdown("### 📋 Candidates")
    st.dataframe(load_candidates_df(), use_container_width=True)
    st.markdown("### 👥 Voters")
    safe = load_voters_df().copy()
    if not safe.empty: safe["token"] = "••••••••"
    st.dataframe(safe, use_container_width=True)
    st.markdown("### 📈 Results")
    st.dataframe(results_df(), use_container_width=True)
