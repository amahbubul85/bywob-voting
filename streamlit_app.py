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
cur.execute("""CREATE TABLE IF NOT EXISTS voters (
    id INTEGER PRIMARY KEY,
    name TEXT,
    email TEXT,
    token TEXT UNIQUE,
    used INTEGER DEFAULT 0,
    used_at TEXT
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    position TEXT,
    candidate TEXT
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY,
    position TEXT,
    candidate TEXT,
    timestamp TEXT
)""")
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
    # Normalize token strings (avoid None issues)
    df["token"] = df["token"].astype(str)
    return df

def load_candidates_df():
    df = pd.read_sql("SELECT * FROM candidates", conn)
    if df.empty:
        return pd.DataFrame(columns=["id","position","candidate"])
    df["position"] = df["position"].astype(str).str.strip()
    df["candidate"] = df["candidate"].astype(str).str.strip()
    return df

def load_votes_df():
    return pd.read_sql("SELECT * FROM votes", conn)

# --------------------------------------------------------------------------------------
# Ops
# --------------------------------------------------------------------------------------
def mark_token_used(token: str):
    cur.execute(
        "UPDATE voters SET used=1, used_at=? WHERE UPPER(TRIM(token))=UPPER(TRIM(?))",
        (now_cet().isoformat(), token),
    )
    conn.commit()

def append_vote(position: str, candidate: str):
    cur.execute(
        "INSERT INTO votes (position,candidate,timestamp) VALUES (?,?,?)",
        (position, candidate, now_cet().isoformat()),
    )
    conn.commit()

def generate_tokens(n: int, prefix: str):
    """Return list of newly generated tokens."""
    alpha = string.ascii_uppercase + string.digits
    new_tokens = []
    for _ in range(int(n)):
        tok = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        try:
            cur.execute(
                "INSERT INTO voters (name,email,token,used,used_at) VALUES (?,?,?,?,?)",
                ("","",tok,0,""),
            )
            new_tokens.append(tok)
        except sqlite3.IntegrityError:
            # Rare collision: try again once
            tok = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
            cur.execute(
                "INSERT OR IGNORE INTO voters (name,email,token,used,used_at) VALUES (?,?,?,?,?)",
                ("","",tok,0,""),
            )
            new_tokens.append(tok)
    conn.commit()
    return new_tokens

def add_voter(name: str, email: str, token: str | None, prefix: str):
    """Add single voter; if token None, auto-generate one."""
    if not token:
        token = generate_tokens(1, prefix)[0]
    else:
        token = token.strip()
        cur.execute(
            "INSERT OR IGNORE INTO voters (name,email,token,used,used_at) VALUES (?,?,?,?,?)",
            (name.strip(), email.strip(), token, 0, ""),
        )
        conn.commit()
    return token

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
            if not token:
                st.error("টোকেন দিন।")
                st.stop()
            if not is_voting_open():
                m = meta_get_all()
                st.error(f"এখন ভোট গ্রহণ করা হচ্ছে না। Status: {m.get('status')}")
                st.stop()

            voters = load_voters_df()
            # case-insensitive, whitespace-tolerant match
            row = voters[voters["token"].str.strip().str.upper() == token.strip().upper()]
            if row.empty:
                st.error("❌ টোকেন সঠিক নয়।")
                st.stop()
            if bool(row["used"].iloc[0] == 1):
                st.error("⚠️ এই টোকেনটি ইতিমধ্যে ব্যবহার করা হয়েছে।")
                st.stop()

            cands = load_candidates_df()
            if cands.empty:
                st.warning("No candidates set")
                st.stop()

            pos_to_cands = {
                p: cands[cands["position"]==p]["candidate"].tolist()
                for p in cands["position"].unique()
            }
            st.session_state.ballot = {
                "ready": True,
                "token": token.strip(),
                "pos_to_cands": pos_to_cands,
            }
            st.rerun()
    else:
        st.success("✅ Token OK. Select your choices below.")
        pos_to_cands = st.session_state.ballot["pos_to_cands"]
        with st.form("full_ballot"):
            for position, options in pos_to_cands.items():
                st.markdown(f"#### {position}")
                st.radio(
                    f"{position} এর জন্য প্রার্থী নির্বাচন করুন:",
                    options,
                    key=f"choice_{position}",
                    index=None,
                    horizontal=True,
                )
            submitted = st.form_submit_button("✅ Submit All Votes")
        if submitted:
            selections = {p: st.session_state.get(f"choice_{p}") for p in pos_to_cands}
            if None in selections.values():
                st.error("সবগুলো পজিশনে ভোট দিন।")
            else:
                for p, c in selections.items():
                    append_vote(p, c)
                mark_token_used(st.session_state.ballot["token"])
                st.success("আপনার সকল ভোট গ্রহণ করা হয়েছে।")
                st.session_state.ballot = {"ready": False}
                st.rerun()

# ------------------------ Results Tab ------------------------
with tab_results:
    st.subheader("📊 Live Results")
    r = results_df()
    st.dataframe(r if not r.empty else pd.DataFrame([{"info":"No votes yet"}]), width='stretch')

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
        meta_set("name", ename)
        meta_set("start_cet", start_dt.isoformat())
        meta_set("end_cet", end_dt.isoformat())
        meta_set("status","scheduled")
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

    # -------------------- Token generator --------------------
    st.markdown("### 🔑 Generate tokens")
    g1, g2 = st.columns(2)
    num = g1.number_input("কতটি টোকেন?", min_value=1, value=10)
    pref = g2.text_input("Prefix", value="BYWOB-2025")
    if st.button("Generate"):
        new_tokens = generate_tokens(num, pref)
        st.success(f"{len(new_tokens)} tokens generated")
        st.caption("Copy the tokens below:")
        st.code("\n".join(new_tokens) if new_tokens else "—")

    # -------------------- Add single voter --------------------
    st.markdown("### ➕ Add single voter")
    with st.form("add_single_voter"):
        sv1, sv2 = st.columns(2)
        name_in = sv1.text_input("Name")
        email_in = sv2.text_input("Email")
        sv3, sv4 = st.columns(2)
        manual_token = sv3.text_input("Token (optional, leave blank to auto-generate)")
        auto_prefix = sv4.text_input("Auto prefix (if blank token)", value="BYWOB-2025")
        add_clicked = st.form_submit_button("Add voter")
    if add_clicked:
        tok = add_voter(name_in, email_in, manual_token or None, auto_prefix)
        st.success(f"Voter added. Token: {tok}")
        st.code(tok)

    # -------------------- Candidates --------------------
    st.markdown("### 📋 Candidates")
    with st.form("add_candidate"):
        col1, col2 = st.columns(2)
        pos = col1.text_input("Position", placeholder="e.g., President")
        cand = col2.text_input("Candidate", placeholder="e.g., Alice")
        submitted = st.form_submit_button("Add Candidate")
    if submitted:
        if pos.strip() and cand.strip():
            cur.execute("INSERT INTO candidates (position, candidate) VALUES (?, ?)", (pos.strip(), cand.strip()))
            conn.commit()
            st.success(f"Candidate '{cand}' added for position '{pos}'")
            st.rerun()
        else:
            st.error("Please enter both position and candidate name.")
    st.dataframe(load_candidates_df(), width='stretch')

    # -------------------- Voters (show/hide tokens + export) --------------------
    st.markdown("### 👥 Voters")
    show_tokens = st.checkbox("Show tokens", value=False, help="Unmask tokens for copying/exporting.")
    voters_df = load_voters_df()
    if voters_df.empty:
        st.info("কোনো ভোটার নেই।")
    else:
        display_df = voters_df.copy()
        if not show_tokens:
            display_df["token"] = "••••••••"
        st.dataframe(display_df[["id","name","email","token","used","used_at"]], width='stretch')

        # Download full tokens CSV (always includes real tokens)
        csv_bytes = voters_df[["id","name","email","token","used","used_at"]].to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download tokens.csv",
            data=csv_bytes,
            file_name="tokens.csv",
            mime="text/csv",
        )

    # -------------------- Results --------------------
    st.markdown("### 📈 Results")
    st.dataframe(results_df(), width='stretch')
