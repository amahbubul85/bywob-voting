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
    st.subheader("🛠️ Admin")

    # ===== Helpers for voters CRUD =====
    def update_voter(voter_id: int, name: str, email: str, token: str | None):
        cur.execute(
            "UPDATE voters SET name=?, email=?, token=? WHERE id=?",
            (name.strip(), email.strip(), (token or "").strip(), int(voter_id)),
        )
        conn.commit()

    def delete_voter(voter_id: int):
        cur.execute("DELETE FROM voters WHERE id=?", (int(voter_id),))
        conn.commit()

    # ===== Election controls (unchanged) =====
    m = meta_get_all()
    st.markdown(
        f"**Status:** `{m.get('status')}`  |  **Start:** `{m.get('start_cet')}`  |  **End:** `{m.get('end_cet')}`  |  **Published:** `{m.get('published')}`"
    )
    st.divider()

    # ===== Sub-tabs for administration =====
    sub_tab_voters, sub_tab_upload, sub_tab_tokens, sub_tab_schedule = st.tabs(
        ["👥 Voters", "⬆️ Upload CSV", "🔑 Tokens", "🗓️ Schedule"]
    )

    # -------------------------------------------------------------------------
    # 👥 Voters – add / edit / delete
    # -------------------------------------------------------------------------
    with sub_tab_voters:
        st.markdown("### Manage voters")
        st.caption("Add, edit, or delete voters. Tokens can be edited here as text.")

        # Add new voter (single)
        with st.form("add_voter_inline"):
            c1, c2, c3 = st.columns([2, 2, 2])
            name_new = c1.text_input("Name")
            email_new = c2.text_input("Email")
            token_new = c3.text_input("Token (optional)")
            pr = st.text_input("Auto prefix (if token left blank)", value="BYWOB-2025")
            add_click = st.form_submit_button("Add voter")
        if add_click:
            tok = add_voter(name_new, email_new, token_new or None, pr)
            st.success(f"Added voter. Token: {tok}")
            st.rerun()

        st.markdown("---")

        # Editable grid of voters
        voters_df = load_voters_df()
        if voters_df.empty:
            st.info("No voters yet. Add with the form above or use the Upload CSV tab.")
        else:
            # Keep a copy to detect changes/deletions
            original = voters_df.copy()

            # Display editable table (id is frozen/not editable)
            edited = st.data_editor(
                voters_df[["id", "name", "email", "token", "used", "used_at"]],
                column_config={
                    "id": st.column_config.NumberColumn("ID", disabled=True),
                    "used": st.column_config.CheckboxColumn("Used", disabled=True),
                    "used_at": st.column_config.TextColumn("Used at", disabled=True),
                },
                hide_index=True,
                num_rows="dynamic",
                width="stretch",
            )

            # Apply edits & detect deletes
            # 1) Rows that were removed:
            removed_ids = set(original["id"]) - set(edited["id"]) if not edited.empty else set(original["id"])
            if removed_ids:
                if st.button(f"🗑️ Delete {len(removed_ids)} removed row(s)"):
                    for rid in removed_ids:
                        delete_voter(int(rid))
                    st.success(f"Deleted {len(removed_ids)} voter(s).")
                    st.rerun()

            # 2) Apply edits to existing rows (id matches)
            if st.button("💾 Save edits"):
                changed = 0
                # join edited on id to compare
                merged = edited.merge(original, on="id", how="left", suffixes=("", "_orig"))
                for _, r in merged.iterrows():
                    if pd.isna(r["id"]):
                        continue
                    # If there's any change in name/email/token
                    if (
                        str(r["name"]) != str(r["name_orig"])
                        or str(r["email"]) != str(r["email_orig"])
                        or str(r["token"]) != str(r["token_orig"])
                    ):
                        update_voter(
                            int(r["id"]),
                            str(r["name"] or ""),
                            str(r["email"] or ""),
                            str(r["token"] or ""),
                        )
                        changed += 1
                if changed:
                    st.success(f"Saved {changed} change(s).")
                    st.rerun()
                else:
                    st.info("No changes to save.")

            # Export
            csv_bytes = voters_df[["id", "name", "email", "token", "used", "used_at"]].to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download voters (CSV)", data=csv_bytes, file_name="voters.csv", mime="text/csv")

    # -------------------------------------------------------------------------
    # ⬆️ Upload CSV – name,email[,token]
    # -------------------------------------------------------------------------
    with sub_tab_upload:
        st.markdown("### Bulk upload voters (CSV)")
        st.caption("Required columns: **name,email**. Optional column: **token**. Missing tokens will be auto-generated.")
        up_file = st.file_uploader("Upload CSV file", type=["csv"])
        auto_prefix = st.text_input("Auto prefix for blank tokens", value="BYWOB-2025", key="bulk_prefix")
        if up_file is not None:
            import io
            df_in = pd.read_csv(io.StringIO(up_file.getvalue().decode("utf-8"))).fillna("")
            # Validate columns
            if not {"name", "email"}.issubset(df_in.columns):
                st.error("CSV must include columns: name, email (token is optional).")
            else:
                if "token" not in df_in.columns:
                    df_in["token"] = ""
                st.dataframe(df_in, width="stretch")
                if st.button("📥 Import voters"):
                    created = 0
                    for _, r in df_in.iterrows():
                        tok = add_voter(str(r["name"]), str(r["email"]), (str(r["token"]) or None), auto_prefix)
                        created += 1
                    st.success(f"Imported {created} voters.")
                    st.rerun()

    # -------------------------------------------------------------------------
    # 🔑 Tokens – generate & assign
    # -------------------------------------------------------------------------
    with sub_tab_tokens:
        st.markdown("### Generate tokens")
        c1, c2 = st.columns(2)
        n = c1.number_input("How many", min_value=1, value=20, step=10)
        prefix = c2.text_input("Prefix", value="BYWOB-2025")
        if st.button("Generate"):
            toks = generate_tokens(int(n), prefix)
            st.success(f"{len(toks)} tokens generated.")
            st.code("\n".join(toks) if toks else "—")

        st.markdown("---")
        st.markdown("### Assign tokens to voters without one")
        st.caption("Auto-fills tokens for voters where token is empty.")
        voters_no_token = load_voters_df()
        voters_no_token = voters_no_token[voters_no_token["token"].astype(str).str.strip() == ""]
        st.write(f"Voters without token: **{len(voters_no_token)}**")
        assign_n = st.number_input("Assign how many now", min_value=1, value=min(10, len(voters_no_token)), step=1)
        assign_prefix = st.text_input("Assign prefix", value="BYWOB-2025", key="assign_prefix")
        if st.button("Assign now", disabled=len(voters_no_token) == 0):
            to_assign = voters_no_token.head(int(assign_n))
            new_list = generate_tokens(len(to_assign), assign_prefix)
            for (idx, row), tok in zip(to_assign.iterrows(), new_list):
                update_voter(int(row["id"]), str(row["name"] or ""), str(row["email"] or ""), tok)
            st.success(f"Assigned {len(new_list)} token(s).")
            st.rerun()

    # -------------------------------------------------------------------------
    # 🗓️ Schedule – (kept simple, same logic you had)
    # -------------------------------------------------------------------------
    with sub_tab_schedule:
        st.markdown("### Election schedule")
        ename = st.text_input("Election name", value=m.get("name", ""))

        def _p(s):
            try: return datetime.fromisoformat(s)
            except: return None

        sdt_saved = _p(m.get("start_cet","")) or now_cet()
        edt_saved = _p(m.get("end_cet","")) or (now_cet() + timedelta(hours=2))

        left, right = st.columns(2)
        with left:
            sdate = st.date_input("Start date (CET)", value=sdt_saved.date())
            stime = st.time_input("Start time (CET)", value=sdt_saved.time().replace(second=0, microsecond=0))
        with right:
            edate = st.date_input("End date (CET)", value=edt_saved.date())
            etime = st.time_input("End time (CET)", value=edt_saved.time().replace(second=0, microsecond=0))

        start_dt = datetime.combine(sdate, stime).replace(tzinfo=CET)
        end_dt   = datetime.combine(edate, etime).replace(tzinfo=CET)

        st.info(f"Start: {start_dt.strftime('%Y-%m-%d %H:%M')}  |  End: {end_dt.strftime('%Y-%m-%d %H:%M')}")

        colA, colB, colC = st.columns(3)
        if colA.button("Set & Schedule"):
            if end_dt <= start_dt:
                st.error("End time must be after start time.")
            else:
                meta_set("name", ename)
                meta_set("start_cet", start_dt.isoformat())
                meta_set("end_cet", end_dt.isoformat())
                meta_set("status", "scheduled")
                meta_set("published", "FALSE")
                st.success("Scheduled (status = scheduled).")
                st.rerun()

        if colB.button("Start Now"):
            start_now = now_cet()
            end_kept = _p(meta_get_all().get("end_cet","")) or (start_now + timedelta(hours=2))
            if end_kept <= start_now:
                end_kept = start_now + timedelta(hours=2)
            meta_set("name", ename)
            meta_set("start_cet", start_now.isoformat())
            meta_set("end_cet", end_kept.isoformat())
            meta_set("status", "ongoing")
            st.success(f"Started now. Ends {end_kept.strftime('%Y-%m-%d %H:%M CET')}.")
            st.rerun()

        if colC.button("End Now"):
            end_now = now_cet()
            meta_set("end_cet", end_now.isoformat())
            meta_set("status", "ended")
            st.success(f"Ended at {end_now.strftime('%Y-%m-%d %H:%M CET')}.")
            st.rerun()
