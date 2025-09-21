import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone, date, time

# -----------------------------
# Google Sheets Auth
# -----------------------------
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    st.secrets["gcp_service_account"], scope)
client = gspread.authorize(creds)

try:
    SHEET_ID = st.secrets["gcp_service_account"]["SHEET_ID"]
    sheet = client.open_by_key(SHEET_ID)
except Exception as e:
    st.error(f"❌ Google Sheet open করতে পারিনি: {e}")
    st.stop()

# -----------------------------
# Helper Functions
# -----------------------------
def get_meta():
    ws = sheet.worksheet("meta")
    rows = ws.get_all_records()
    return {r["key"]: r["value"] for r in rows}

def set_meta(key, value):
    ws = sheet.worksheet("meta")
    try:
        cells = ws.findall(key)
        if cells:
            ws.update_cell(cells[0].row, 2, value)
        else:
            ws.append_row([key, value])
    except Exception as e:
        st.error(f"Meta update error: {e}")

# -----------------------------
# Tabs
# -----------------------------
tab1, tab2, tab3, tab4 = st.tabs(
    ["🗳️ Vote", "📊 Results", "👨‍💻 Admin", "ℹ️ Info"]
)

# -----------------------------
# Vote Tab
# -----------------------------
with tab1:
    st.header("🗳️ Cast Your Vote")

    token = st.text_input("Enter your voting token")
    if token:
        voters_ws = sheet.worksheet("voters")
        voters = voters_ws.get_all_records()
        df_voters = pd.DataFrame(voters)

        df_voters['token'] = df_voters['token'].astype(str).str.strip()
        df_voters['used_bool'] = df_voters['used'].astype(str).str.lower().isin(['true', '1', 'yes'])

        row = df_voters[df_voters['token'] == token.strip()]

        if not row.empty and not row['used_bool'].iloc[0]:
            candidates = sheet.worksheet("candidates").get_all_records()
            df_cand = pd.DataFrame(candidates)

            pos = st.selectbox("Choose Position", df_cand['position'].unique())
            cand_list = df_cand[df_cand['position'] == pos]['candidate'].tolist()
            cand = st.selectbox("Choose Candidate", cand_list)

            if st.button("Submit Vote"):
                votes_ws = sheet.worksheet("votes")
                votes_ws.append_row([pos, cand, str(datetime.now(timezone.utc))])
                idx = row.index[0] + 2
                voters_ws.update_cell(idx, 4, "TRUE")
                voters_ws.update_cell(idx, 5, datetime.now(timezone.utc).isoformat())
                st.success("✅ Vote submitted successfully!")
        else:
            st.error("❌ Invalid or already used token")

# -----------------------------
# Results Tab
# -----------------------------
with tab2:
    st.header("📊 Live Results")
    votes = sheet.worksheet("votes").get_all_records()
    if votes:
        df_votes = pd.DataFrame(votes)
        results = df_votes.groupby(["position", "candidate"]).size().reset_index(name="votes")
        st.dataframe(results, width="stretch")
    else:
        st.info("No votes yet.")

# -----------------------------
# Admin Tab
# -----------------------------
with tab3:
    st.header("👨‍💻 Admin Panel")

    # Election metadata
    meta = get_meta()
    st.markdown("#### Create / Schedule new election")

    now_ = datetime.now(timezone.utc)
    c1, c2 = st.columns(2)

    start_date = c1.date_input("Start date (UTC)", value=now_.date())
    start_time = c1.time_input("Start time (UTC)", value=now_.time().replace(microsecond=0))
    end_date = c2.date_input("End date (UTC)", value=now_.date())
    end_time = c2.time_input("End time (UTC)", value=now_.time().replace(microsecond=0))

    start_dt = datetime.combine(start_date, start_time).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, end_time).replace(tzinfo=timezone.utc)

    ename = st.text_input("Election name", value=meta.get("name", ""))

    if st.button("Set & Schedule"):
        set_meta("name", ename)
        set_meta("start_utc", start_dt.isoformat())
        set_meta("end_utc", end_dt.isoformat())
        set_meta("status", "idle")
        set_meta("published", "FALSE")
        st.success("✅ Election scheduled successfully!")

# -----------------------------
# Info Tab
# -----------------------------
with tab4:
    st.header("ℹ️ Info")
    st.write("This is a demo online voting platform for BYWOB.")
