# streamlit_app.py
# BYWOB Online Voting — Streamlit + SQLite (no Google Sheets quota issues)

import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import secrets, string

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="centered")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + SQLite • Secret ballot with one-time tokens")

# --------------------------------------------------------------------------------------
# Local timezone (handles DST automatically)
# --------------------------------------------------------------------------------------
CET = ZoneInfo("Europe/Paris")  # auto-switches CET/CEST

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

cur.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
cur.execute(
    """CREATE TABLE IF NOT EXISTS voters (
        id INTEGER PRIMARY KEY,
        name TEXT,
        email TEXT,
        token TEXT UNIQUE,
        used INTEGER DEFAULT 0,
        used_at TEXT
    )"""
)
cur.execute(
    """CREATE TABLE IF NOT EXISTS candidates (
        id INTEGER PRIMARY KEY,
        position TEXT,
        candidate TEXT
    )"""
)
cur.execute(
    """CREATE TABLE IF NOT EXISTS votes (
        id INTEGER PRIMARY KEY,
        position TEXT,
        candidate TEXT,
        timestamp TEXT
    )"""
)

cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_candidates ON candidates(position, candidate)")
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_voters_email ON voters(email)")
conn.commit()

# --------------------------------------------------------------------------------------
# Meta helpers
# --------------------------------------------------------------------------------------
def meta_get_all() -> dict:
    return dict(cur.execute("SELECT key,value FROM meta").fetchall())

def meta_set(k: str, v: str):
    cur.execute("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (k, v))
    conn.commit()

m0 = meta_get_all()
defaults = {
    "status": "idle",
    "name": "",
    "start_cet": "",
    "end_cet": "",
    "published": "FALSE",
}
for k, v in defaults.items():
    if k not in m0:
        meta_set(k, v)

def is_voting_open() -> bool:
    m = meta_get_all()
    if m.get("status", "idle") != "ongoing":
        return False
    try:
        sdt = datetime.fromisoformat(m.get("start_cet", "")) if m.get("start_cet") else None
        edt = datetime.fromisoformat(m.get("end_cet", "")) if m.get("end_cet") else None
    except Exception:
        return False
    now = now_cet()
    if sdt and now < to_cet(sdt):
        return False
    if edt and now > to_cet(edt):
        meta_set("status", "ended")
        return False
    return True

# --------------------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------------------
def load_voters_df():
    df = pd.read_sql("SELECT * FROM voters", conn)
    if df.empty:
        return pd.DataFrame(columns=["id", "name", "email", "token", "used", "used_at", "used_bool"])
    df["used_bool"] = df["used"] == 1
    df["token"] = df["token"].astype(str)
    return df

def load_candidates_df():
    df = pd.read_sql("SELECT * FROM candidates", conn)
    if df.empty:
        return pd.DataFrame(columns=["id", "position", "candidate"])
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
    alpha = string.ascii_uppercase + string.digits
    new_tokens = []
    for _ in range(int(n)):
        while True:
            tok = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
            try:
                cur.execute(
                    "INSERT INTO voters (name,email,token,used,used_at) VALUES (?,?,?,?,?)",
                    ("", "", tok, 0, ""),
                )
                conn.commit()
                new_tokens.append(tok)
                break
            except sqlite3.IntegrityError:
                continue
    return new_tokens

def results_df():
    df = load_votes_df()
    if df.empty:
        return pd.DataFrame(columns=["position", "candidate", "votes"])
    g = df.groupby(["position", "candidate"]).size().reset_index(name="votes")
    return g.sort_values(["position", "votes"], ascending=[True, False])

# ---- Candidate CRUD helpers ----
def add_candidate(position: str, candidate: str):
    cur.execute(
        "INSERT OR IGNORE INTO candidates (position, candidate) VALUES (?, ?)",
        (position.strip(), candidate.strip()),
    )
    conn.commit()

def update_candidate(row_id: int, position: str, candidate: str):
    cur.execute(
        "UPDATE candidates SET position = ?, candidate = ? WHERE id = ?",
        (position.strip(), candidate.strip(), int(row_id)),
    )
    conn.commit()

def delete_candidate(row_id: int):
    cur.execute("DELETE FROM candidates WHERE id = ?", (int(row_id),))
    conn.commit()

# ---- Voter CRUD helpers ----
def get_voter_by_email(email: str):
    return cur.execute("SELECT * FROM voters WHERE email = ?", (email.strip(),)).fetchone()

def upsert_voter(name: str, email: str, token: str | None, prefix: str) -> str:
    name = (name or "").strip()
    email = (email or "").strip()
    tok = (token or "").strip()
    if not email:
        raise ValueError("Email is required")
    if not tok:
        tok = generate_tokens(1, prefix)[0]
    existing = cur.execute("SELECT id FROM voters WHERE email = ?", (email,)).fetchone()
    if existing:
        cur.execute("UPDATE voters SET name=?, token=? WHERE id=?", (name, tok, int(existing[0])))
    else:
        cur.execute(
            "INSERT INTO voters (name,email,token,used,used_at) VALUES (?,?,?,?,?)",
            (name, email, tok, 0, ""),
        )
    conn.commit()
    return tok

def update_voter(row_id: int, name: str, email: str, token: str):
    cur.execute(
        "UPDATE voters SET name=?, email=?, token=? WHERE id=?",
        (name.strip(), email.strip(), token.strip(), int(row_id)),
    )
    conn.commit()

def delete_voter(row_id: int):
    cur.execute("DELETE FROM voters WHERE id=?", (int(row_id),))
    conn.commit()

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
                p: cands[cands["position"] == p]["candidate"].tolist() for p in cands["position"].unique()
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
    st.dataframe(r if not r.empty else pd.DataFrame([{"info": "No votes yet"}]), use_container_width=True)

# ------------------------ Admin Tab ------------------------
with tab_admin:
    st.subheader("🛠️ Admin Tools")
    m = meta_get_all()
    st.markdown(f"**Status:** {m.get('status')}  |  Start: {m.get('start_cet')}  |  End: {m.get('end_cet')}")

    # ---- Schedule election ----
    ename = st.text_input("Election name", value=m.get("name", ""))

    def _parse_iso(s: str):
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    _sdt_saved = _parse_iso(m.get("start_cet", "")) or now_cet()
    _edt_saved = _parse_iso(m.get("end_cet", "")) or (now_cet() + timedelta(hours=2))

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Start Time (CET)**")
        sdt = st.date_input("Start date", value=to_cet(_sdt_saved).date())
        stm = st.time_input("Start time", value=to_cet(_sdt_saved).time().replace(second=0, microsecond=0))
    with col2:
        st.markdown("**End Time (CET)**")
        edt = st.date_input("End date", value=to_cet(_edt_saved).date())
        etm = st.time_input("End time", value=to_cet(_edt_saved).time().replace(second=0, microsecond=0))

    start_dt = datetime.combine(sdt, stm).replace(tzinfo=CET)
    end_dt = datetime.combine(edt, etm).replace(tzinfo=CET)

    if st.button("Set & Schedule"):
        if end_dt <= start_dt:
            st.error("End time must be after start time.")
        elif end_dt <= now_cet():
            st.error("End time already in the past.")
        else:
            meta_set("name", ename)
            meta_set("start_cet", start_dt.isoformat())
            meta_set("end_cet", end_dt.isoformat())
            meta_set("status", "scheduled")
            meta_set("published", "FALSE")
            st.success("Election scheduled.")
            st.rerun()

    c1, c2, c3 = st.columns(3)
    if c1.button("Start Now"):
        start_now = now_cet()
        end_cet_str = meta_get_all().get("end_cet", "")
        try:
            end_kept = datetime.fromisoformat(end_cet_str) if end_cet_str else None
        except Exception:
            end_kept = None
        if not end_kept or end_kept <= start_now:
            end_kept = (start_now + timedelta(hours=2)).replace(second=0, microsecond=0)

        meta_set("name", ename or m.get("name", ""))
        meta_set("start_cet", start_now.isoformat())
        meta_set("end_cet", end_kept.isoformat())
        meta_set("status", "ongoing")
        st.success(f"Election started. Ends {end_kept.strftime('%Y-%m-%d %H:%M')}.")
        st.rerun()

    if c2.button("End Now"):
        end_now = now_cet()
        meta_set("end_cet", end_now.isoformat())
        meta_set("status", "ended")
        st.success(f"Election ended at {end_now.strftime('%Y-%m-%d %H:%M')}.")
        st.rerun()

    if c3.button("Publish Results"):
        meta_set("published", "TRUE"); meta_set("status", "ended")
        st.success("Results published.")

    st.divider()
    st.markdown("### 🗄️ Archive & Reset")
    if st.button("Archive votes & reset voters"):
        ts = now_cet().strftime("%Y%m%dT%H%M%S")
        archive_table = f"votes_archive_{ts}"
        cur.execute(f"CREATE TABLE IF NOT EXISTS {archive_table} AS SELECT * FROM votes")
        conn.commit()
        cur.execute("DELETE FROM votes")
        conn.commit()
        cur.execute("UPDATE voters SET used = 0, used_at = ''")
        conn.commit()
        st.success(f"Archived to {archive_table}, reset tokens.")
        st.rerun()

    # -------------------- Token generator --------------------
    st.markdown("### 🔑 Generate tokens")
    g1, g2 = st.columns(2)
    num = g1.number_input("কতটি টোকেন?", min_value=1, value=10)
    pref = g2.text_input("Prefix", value="BYWOB-2025")
    if st.button("Generate"):
        new_tokens = generate_tokens(num, pref)
        st.success(f"{len(new_tokens)} tokens generated")
        st.code("\n".join(new_tokens))

    # -------------------- CSV import --------------------
    st.markdown("### 📥 Import voters (CSV)")
    st.caption("CSV columns: name,email,[token]")
    csv_file = st.file_uploader("Upload CSV", type=["csv"])
    auto_pref = st.text_input("Auto-token prefix", value="BYWOB-2025")

    if csv_file:
        up_df = pd.read_csv(csv_file).fillna("")
        cols = {c.lower().strip(): c for c in up_df.columns}
        if "name" in cols and "email" in cols:
            token_col = cols.get("token")
            st.dataframe(up_df, use_container_width=True)
            if st.button("Import / Upsert"):
                added, updated = 0, 0
                for _, r in up_df.iterrows():
                    name = str(r[cols["name"]]).strip()
                    email = str(r[cols["email"]]).strip()
                    token = str(r[token_col]).strip() if token_col else ""
                    existed = get_voter_by_email(email)
                    upsert_voter(name, email, token if token else None, auto_pref)
                    if existed: updated += 1
                    else: added += 1
                st.success(f"Imported — {added} added, {updated} updated.")
                st.rerun()
        else:
            st.error("CSV must have name and email columns.")

    # -------------------- Voters --------------------
    st.markdown("### 👥 Voters")
    show_tokens = st.checkbox("Show tokens", value=False)
    voters_df = load_voters_df()
    if voters_df.empty:
        st.info("কোনো ভোটার নেই।")
    else:
        for _, r in voters_df.sort_values("id").iterrows():
            rid = int(r["id"])
            with st.container(border=True):
                vc1, vc2, vc3, vc4, vc5 = st.columns([2,3,3,3,2])
                with vc1:
                    st.caption(f"ID {rid} • Used: {'✅' if r['used']==1 else '—'} {r['used_at']}")
                with vc2:
                    name_val = st.text_input("Name", value=str(r["name"] or ""), key=f"vn_{rid}")
                with vc3:
                    email_val = st.text_input("Email", value=str(r["email"] or ""), key=f"ve_{rid}")
                with vc4:
                    tok_val = st.text_input(
                        "Token",
                        value=str(r["token"]) if show_tokens else "••••••••",
                        key=f"vt_{rid}",
                    )
                    if not show_tokens: tok_val = str(r["token"])
                with vc5:
                    if st.button("Save", key=f"vsave_{rid}"):
                        if not email_val.strip():
                            st.error("Email required")
                        else:
