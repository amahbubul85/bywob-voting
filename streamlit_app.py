# streamlit_app.py
# BYWOB Online Voting — Streamlit + SQLite (no Google Sheets quota issues)

import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo  # ✅ added for proper CET/CEST handling
import secrets, string
import smtplib, ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="centered")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + SQLite • Secret ballot with one-time tokens")

# --------------------------------------------------------------------------------------
# CET Timezone
# --------------------------------------------------------------------------------------
# CET/CEST with DST handled automatically for France:
CET = ZoneInfo("Europe/Paris")  # ✅ replaced fixed UTC+1 with proper zone

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
# optional: prevent exact duplicates (position, candidate)
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_candidates ON candidates(position, candidate)")
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

# -------------------- Email helper --------------------
def send_token_email_smtp(
    receiver_email: str,
    receiver_name: str,
    token: str,
    election_name: str,
    smtp_server: str,
    smtp_port: int,
    sender_email: str,
    sender_password: str,
    sender_name: str = "",
    use_ssl: bool = True,
    subject_template: str = "Your voting token for {election}",
    body_template: str = (
        "Dear {name},\n\n"
        "Here is your secure voting token for {election}:\n\n"
        "    {token}\n\n"
        "Use this token to cast your vote during the election period.\n\n"
        "Regards,\n{sender_name}"
    ),
):
    subject = subject_template.format(election=election_name, name=receiver_name, token=token)
    body = body_template.format(
        election=election_name, name=receiver_name, token=token, sender_name=sender_name or sender_email
    )

    msg = MIMEMultipart()
    msg["From"] = f"{sender_name} <{sender_email}>" if sender_name else sender_email
    msg["To"] = receiver_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    context = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
    else:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls(context=context)
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())


# --------------------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------------------
def load_voters_df():
    df = pd.read_sql("SELECT * FROM voters", conn)
    if df.empty: return pd.DataFrame(columns=["id","name","email","token","used","used_at","used_bool"])
    df["used_bool"] = df["used"]==1
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

# --- Safe token utilities (no dummy inserts) ---
def token_exists(tok: str, exclude_id: int | None = None) -> bool:
    tok = (tok or "").strip()
    if not tok:
        return False
    if exclude_id is None:
        row = cur.execute("SELECT 1 FROM voters WHERE TRIM(token)=TRIM(?) LIMIT 1", (tok,)).fetchone()
    else:
        row = cur.execute(
            "SELECT 1 FROM voters WHERE TRIM(token)=TRIM(?) AND id<>?",
            (tok, int(exclude_id)),
        ).fetchone()
    return row is not None

def create_unique_token(prefix: str) -> str:
    alpha = string.ascii_uppercase + string.digits
    while True:
        tok = prefix + "-" + "".join(secrets.choice(alpha) for _ in range(6))
        if not token_exists(tok):
            return tok


# ---- extra helpers for CSV + bulk upsert ----
def get_voter_by_email(email: str):
    return cur.execute("SELECT id FROM voters WHERE TRIM(LOWER(email)) = TRIM(LOWER(?))", (email.strip(),)).fetchone()

def upsert_voter_by_email(name: str, email: str, token: str | None, auto_prefix: str) -> str:
    """Create/update a voter by email. If token is missing, auto-generate one."""
    name = (name or "").strip()
    email = (email or "").strip()
    tok = (token or "").strip() if token else ""
    if not email:
        return ""

    if not tok:
        tok = generate_tokens(1, auto_prefix)[0]

    existing = get_voter_by_email(email)
    if existing:
        cur.execute("UPDATE voters SET name=?, token=? WHERE id=?", (name, tok, int(existing[0])))
    else:
        cur.execute(
            "INSERT INTO voters (name,email,token,used,used_at) VALUES (?,?,?,?,?)",
            (name, email, tok, 0, ""),
        )
    conn.commit()
    return tok


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

# ✅ Voter edit helpers (added)
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

    # ---- Create / Schedule new election (robust) ----
    ename = st.text_input("Election name", value=m.get("name", ""))

    def _parse_iso(s: str):
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    _sdt_saved = _parse_iso(m.get("start_cet", "")) or now_cet()
    _edt_saved = _parse_iso(m.get("end_cet", ""))   or (now_cet() + timedelta(hours=2))

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Start Time (CET)**")
        sdt = st.date_input("Start date", value=to_cet(_sdt_saved).date(), key="start_date")
        stm = st.time_input(
            "Start time",
            value=to_cet(_sdt_saved).time().replace(second=0, microsecond=0),
            key="start_time",
        )
    with col2:
        st.markdown("**End Time (CET)**")
        edt = st.date_input("End date", value=to_cet(_edt_saved).date(), key="end_date")
        etm = st.time_input(
            "End time",
            value=to_cet(_edt_saved).time().replace(second=0, microsecond=0),
            key="end_time",
        )

    start_dt = datetime.combine(sdt, stm).replace(tzinfo=CET)
    end_dt   = datetime.combine(edt, etm).replace(tzinfo=CET)

    st.info(
        f"**সময়সূচী (CET):**\n"
        f"- শুরু: {start_dt.strftime('%Y-%m-%d %H:%M')}\n"
        f"- শেষ:  {end_dt.strftime('%Y-%m-%d %H:%M')}"
    )

    if st.button("Set & Schedule"):
        if end_dt <= start_dt:
            st.error("End time must be **after** start time.")
        elif end_dt <= now_cet():
            st.error("End time is already **in the past**. Pick a future end.")
        else:
            meta_set("name", ename)
            meta_set("start_cet", start_dt.isoformat())
            meta_set("end_cet",   end_dt.isoformat())
            meta_set("status",    "scheduled")
            meta_set("published", "FALSE")
            st.success("Election scheduled (status = scheduled).")
            st.rerun()

    c1,c2,c3 = st.columns(3)
    if c1.button("Start Now"):
        start_now = now_cet()
        end_cet_str = meta_get_all().get("end_cet", "")
        try:
            end_kept = datetime.fromisoformat(end_cet_str) if end_cet_str else None
        except Exception:
            end_kept = None
        if not end_kept or end_kept <= start_now:
            end_kept = (start_now + timedelta(hours=2)).replace(second=0, microsecond=0)

        meta_set("name", ename or m.get("name",""))
        meta_set("start_cet", start_now.isoformat())
        meta_set("end_cet",   end_kept.isoformat())
        meta_set("status",    "ongoing")
        st.success(f"Election started now. Ends {end_kept.strftime('%Y-%m-%d %H:%M CET')}.")
        st.rerun()

    if c2.button("End Now"):
        end_now = now_cet()
        meta_set("end_cet", end_now.isoformat())
        meta_set("status", "ended")
        st.success(f"Election ended at {end_now.strftime('%Y-%m-%d %H:%M CET')}.")
        st.rerun()

    if c3.button("Publish Results"):
        meta_set("published","TRUE"); meta_set("status","ended")
        st.success("Results published")

    st.divider()
    st.markdown("### 🗄️ Archive & Reset for a New Election")

    if st.button("Archive votes & reset voters"):
        ts = now_cet().strftime("%Y%m%dT%H%M%S")
        archive_table = f"votes_archive_{ts}"
        cur.execute(f"CREATE TABLE IF NOT EXISTS {archive_table} AS SELECT * FROM votes")
        conn.commit()
        cur.execute("DELETE FROM votes")
        conn.commit()
        cur.execute("UPDATE voters SET used = 0, used_at = ''")
        conn.commit()
        st.success(f"Archived current votes to table: {archive_table} and reset tokens.")
        st.rerun()

    st.divider()
st.markdown("### 📧 Email voters their tokens")

with st.expander("SMTP settings (not saved)"):
    colA, colB = st.columns(2)
    with colA:
        smtp_server = st.text_input("SMTP server", value="smtp.gmail.com", help="Gmail: smtp.gmail.com")
        smtp_port   = st.number_input("Port", value=465, step=1, help="SSL: 465, STARTTLS: 587")
        use_ssl     = st.radio("Security", options=["SSL (recommended)", "STARTTLS"], horizontal=True) == "SSL (recommended)"
    with colB:
        sender_email    = st.text_input("Sender email (login)")
        sender_password = st.text_input("App password / SMTP key", type="password", help="For Gmail, use an App Password")
        sender_name     = st.text_input("Sender display name", value="Election Committee")

    m = meta_get_all()
    election_name = st.text_input("Election name (for emails)", value=m.get("name", "BYWOB Election"))

    st.caption("Subject/body support {name}, {token}, {election}, {sender_name}")
    subj = st.text_input("Subject", value="Your voting token for {election}")
    body = st.text_area(
        "Body",
        value=(
            "Dear {name},\n\n"
            "Here is your secure voting token for {election}:\n\n"
            "    {token}\n\n"
            "Use this token to cast your vote during the election period.\n\n"
            "Regards,\n{sender_name}"
        ),
        height=160,
    )

    # Optional preview on the first voter
    voters_preview = load_voters_df()
    if not voters_preview.empty:
        prv = voters_preview.iloc[0]
        st.markdown("**Preview:**")
        st.code(
            f"To: {prv['email']}\n"
            f"Subject: {subj.format(election=election_name, name=str(prv['name'] or ''), token=str(prv['token']))}\n\n"
            f"{body.format(election=election_name, name=str(prv['name'] or ''), token=str(prv['token']), sender_name=sender_name)}"
        )




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


    
    # -------------------- Candidates (CSV replace + inline add/edit/delete) --------------------
    st.markdown("### 📋 Candidates (persisted)")

    cl, cr = st.columns([3, 2])

    with cr:
        cand_csv = st.file_uploader("Upload Candidates CSV (position,candidate)", type=["csv"])
        if cand_csv is not None:
            try:
                cdf = pd.read_csv(cand_csv).fillna("")
                cols = {c.lower().strip(): c for c in cdf.columns}
                if "position" not in cols or "candidate" not in cols:
                    st.error("CSV must contain at least: position, candidate")
                else:
                    pcol, ccol = cols["position"], cols["candidate"]

                    # Clean + dedupe
                    imp = cdf[[pcol, ccol]].copy()
                    imp[pcol] = imp[pcol].astype(str).str.strip()
                    imp[ccol] = imp[ccol].astype(str).str.strip()
                    imp = imp[(imp[pcol] != "") & (imp[ccol] != "")]
                    imp = imp.drop_duplicates([pcol, ccol], keep="last")

                    # ✅ REPLACE: wipe and insert fresh
                    cur.execute("DELETE FROM candidates")
                    conn.commit()

                    for _, r in imp.iterrows():
                        cur.execute(
                            "INSERT OR IGNORE INTO candidates (position, candidate) VALUES (?, ?)",
                            (r[pcol], r[ccol]),
                        )
                    conn.commit()
                    st.success(f"Imported {len(imp)} candidates (replaced previous list).")
                    st.rerun()
            except Exception as e:
                st.error(f"CSV read failed: {e}")

    # Load for inline editor
    cand_df = load_candidates_df()
    c_cols = ["id", "position", "candidate"]
    edit_df = cand_df[c_cols].copy() if not cand_df.empty else pd.DataFrame(columns=c_cols)

    st.caption("Add new rows at the bottom. Leave **id** blank for new candidates.")
    c_edited = st.data_editor(
        edit_df,
        key="candidates_editor",
        use_container_width=True,
        num_rows="dynamic",   # <-- add rows inline
        disabled=["id"],      # id is auto PK
    )

    # Save changes (updates + inserts)
    if st.button("💾 Save candidate changes"):
        try:
            # updates: rows with id
            to_update = c_edited[c_edited["id"].notna()].copy()
            for _, r in to_update.iterrows():
                rid = int(r["id"])
                pos = str(r.get("position", "")).strip()
                can = str(r.get("candidate", "")).strip()
                if pos and can:
                    update_candidate(rid, pos, can)

            # inserts: rows without id
            to_insert = c_edited[c_edited["id"].isna()].copy()
            inserted = 0
            for _, r in to_insert.iterrows():
                pos = str(r.get("position", "")).strip()
                can = str(r.get("candidate", "")).strip()
                if not pos and not can:
                    continue  # ignore empty line
                if pos and can:
                    add_candidate(pos, can)
                    inserted += 1

            conn.commit()
            st.success(f"Saved. {len(to_update)} updated, {inserted} added.")
            st.rerun()
        except Exception as e:
            st.error(f"Save failed: {e}")

    # Delete selected
    if not cand_df.empty:
        st.divider()
        st.markdown("#### 🗑️ Delete selected")
        lab_df = cand_df.copy()
        lab_df["_label"] = lab_df["position"].astype(str) + " — " + lab_df["candidate"].astype(str)
        del_ids = st.multiselect(
            "Choose candidates to delete",
            options=lab_df["id"].tolist(),
            format_func=lambda x: lab_df.loc[lab_df["id"] == x, "_label"].values[0],
        )
        if st.button("Delete selected"):
            for rid in del_ids:
                delete_candidate(int(rid))
            conn.commit()
            st.warning(f"Deleted {len(del_ids)} candidate(s).")
            st.rerun()



    # -------------------- Voters (always editable; add rows inline; CSV replaces) --------------------
    st.markdown("### 👥 Voters")

    c_left, c_right = st.columns([3, 2])
    with c_left:
        show_tokens = st.checkbox("Show tokens", value=False, help="Unmask tokens to edit/copy.")

    with c_right:
        csv_file  = st.file_uploader("Upload CSV (name,email[,token])", type=["csv"])
        auto_pref = st.text_input("Auto-token prefix", value="BYWOB-2025")

        if csv_file is not None:
            try:
                up_df = pd.read_csv(csv_file).fillna("")
                cols = {c.lower().strip(): c for c in up_df.columns}
                if "name" not in cols or "email" not in cols:
                    st.error("CSV must contain at least: name, email")
                else:
                    name_c  = cols["name"]
                    email_c = cols["email"]
                    token_c = cols.get("token")  # optional

                    # Replace everything
                    cur.execute("DELETE FROM voters")
                    conn.commit()

                    inserted, seen = 0, set()
                    for _, r in up_df.iterrows():
                        name  = str(r[name_c]).strip()
                        email = str(r[email_c]).strip()
                        tok   = str(r[token_c]).strip() if token_c else ""
                        if not tok or tok in seen or token_exists(tok):
                            tok = create_unique_token(auto_pref)
                        seen.add(tok)
                        cur.execute(
                            "INSERT INTO voters (name,email,token,used,used_at) VALUES (?,?,?,?,?)",
                            (name, email, tok, 0, ""),
                        )
                        inserted += 1

                    conn.commit()
                    st.success(f"Imported {inserted} voters (replaced previous list).")
                    st.rerun()
            except Exception as e:
                st.error(f"CSV read failed: {e}")

    # Load & show editor
    voters_df = load_voters_df()
    cols = ["id","name","email","token","used","used_at"]

    if voters_df.empty:
        df_for_edit = pd.DataFrame(columns=cols)
    else:
        df_for_edit = voters_df[cols].copy()

    # Show masked tokens visually if needed (editor itself uses real values below)
    display_df = df_for_edit.copy()
    if not show_tokens and not display_df.empty:
        display_df["token"] = "••••••••"

    # Always editable table; can add rows at the bottom
    edited = st.data_editor(
        df_for_edit,                      # real values so edits persist
        key="voters_editor",
        use_container_width=True,
        num_rows="dynamic",               # <-- enables adding new rows inline
        disabled=["id","used","used_at"], # system-managed columns
    )

    if st.button("💾 Save changes"):
        try:
            # Updates: rows with an id
            to_update = edited[edited["id"].notna()].copy()
            for _, r in to_update.iterrows():
                rid   = int(r["id"])
                name  = str(r.get("name","")).strip()
                email = str(r.get("email","")).strip()
                tok   = str(r.get("token","")).strip()
                if not tok or token_exists(tok, exclude_id=rid):
                    tok = create_unique_token(auto_pref)
                cur.execute(
                    "UPDATE voters SET name=?, email=?, token=? WHERE id=?",
                    (name, email, tok, rid),
                )

            # Inserts: rows with blank/NaN id (added inline)
            to_insert = edited[edited["id"].isna()].copy()
            for _, r in to_insert.iterrows():
                name  = str(r.get("name","")).strip()
                email = str(r.get("email","")).strip()
                tok   = str(r.get("token","")).strip()
                if not name and not email and not tok:
                    continue  # skip fully empty lines
                if not tok or token_exists(tok):
                    tok = create_unique_token(auto_pref)
                cur.execute(
                    "INSERT INTO voters (name,email,token,used,used_at) VALUES (?,?,?,?,?)",
                    (name, email, tok, 0, ""),
                )

            conn.commit()
            st.success("Voter table saved.")
            st.rerun()
        except Exception as e:
            st.error(f"Save failed: {e}")

    # Export (always real tokens)
    export_df = load_voters_df()
    csv_bytes = export_df[cols].to_csv(index=False).encode("utf-8")
    st.download_button("Download voters.csv", data=csv_bytes, file_name="voters.csv", mime="text/csv")


    # -------------------- Email voters --------------------
    st.markdown("### 📧 Email voters")

    with st.expander("SMTP settings (not saved)"):
        colA, colB = st.columns(2)
        with colA:
            smtp_server = st.text_input("SMTP server", value="smtp.gmail.com")
            smtp_port   = st.number_input("Port", value=465, step=1)
            use_ssl     = st.radio("Security", options=["SSL", "STARTTLS"], index=0) == "SSL"
        with colB:
            sender_email    = st.text_input("Sender email (login)")
            sender_password = st.text_input("App password / SMTP key", type="password")
            sender_name     = st.text_input("Sender name", value="Election Committee")

        m = meta_get_all()
        election_name = st.text_input("Election name (for emails)", value=m.get("name", "BYWOB Election"))

        subj = st.text_input("Subject", value="Your voting token for {election}")
        body = st.text_area(
            "Body",
            value=(
                "Dear {name},\n\n"
                "Here is your secure voting token for {election}:\n\n"
                "    {token}\n\n"
                "Regards,\n{sender_name}"
            ),
            height=160,
        )

    # Send button
    if st.button("🚀 Send tokens by email to all voters with an email"):
        if not sender_email or not sender_password or not smtp_server or not smtp_port:
            st.error("Please fill SMTP server, port, sender email, and password.")
        else:
            voters = load_voters_df()
            if voters.empty:
                st.warning("No voters found.")
            else:
                sent_ok, sent_fail = 0, []
                for _, r in voters.iterrows():
                    email = str(r.get("email","")).strip()
                    token = str(r.get("token","")).strip()
                    name  = str(r.get("name","")).strip()
                    if not email:
                        continue
                    try:
                        send_token_email_smtp(
                            receiver_email=email,
                            receiver_name=name,
                            token=token,
                            election_name=election_name,
                            smtp_server=smtp_server,
                            smtp_port=int(smtp_port),
                            sender_email=sender_email,
                            sender_password=sender_password,
                            sender_name=sender_name,
                            use_ssl=use_ssl,
                            subject_template=subj,
                            body_template=body,
                        )
                        sent_ok += 1
                    except Exception as e:
                        sent_fail.append((email, str(e)))
                if sent_ok:
                    st.success(f"Emails sent to {sent_ok} voter(s).")
                if sent_fail:
                    st.error(f"Failed for {len(sent_fail)}:")
                    for em, err in sent_fail[:10]:
                        st.write(f"- {em}: {err}")
                    if len(sent_fail) > 10:
                        st.write("...and more.")

    # -------------------- Results --------------------
    st.markdown("### 📈 Results")
    st.dataframe(results_df(), width='stretch')
