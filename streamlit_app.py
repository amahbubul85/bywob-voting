import streamlit as st
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
from datetime import datetime

# Google Sheets Connect
scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(st.secrets["gcp_service_account"]["SHEET_ID"])

# Tabs
tab1, tab2, tab3 = st.tabs(["🗳️ Vote", "📊 Results", "🔑 Admin"])

# Vote Tab
with tab1:
    st.header("Vote")
    token = st.text_input("Enter your token")
    if st.button("Proceed") and token:
        voters = sheet.worksheet("voters").get_all_records()
        df_voters = pd.DataFrame(voters)
        if token in df_voters['token'].values and not df_voters[df_voters['token']==token]['used'].values[0]:
            position = st.selectbox("Choose Position", [row['position'] for row in sheet.worksheet("candidates").get_all_records()])
            candidate = st.selectbox("Choose Candidate", [row['candidate'] for row in sheet.worksheet("candidates").get_all_records() if row['position']==position])
            if st.button("Submit Vote"):
                votes_ws = sheet.worksheet("votes")
                votes_ws.append_row([position, candidate, str(datetime.now())])
                idx = df_voters[df_voters['token']==token].index[0] + 2
                sheet.worksheet("voters").update_cell(idx, 4, "TRUE")
                sheet.worksheet("voters").update_cell(idx, 5, str(datetime.now()))
                st.success("✅ Vote submitted!")
        else:
            st.error("Invalid or already used token")

# Results Tab
with tab2:
    st.header("Live Results")
    votes = sheet.worksheet("votes").get_all_records()
    if votes:
        df_votes = pd.DataFrame(votes)
        st.dataframe(df_votes.groupby(["position","candidate"]).size().reset_index(name="Votes"))
    else:
        st.info("No votes yet.")

# Admin Tab
with tab3:
    st.header("Admin Tools")
    if "ADMIN_PASSWORD" in st.secrets:
        pwd = st.text_input("Password", type="password")
        if pwd != st.secrets["ADMIN_PASSWORD"]:
            st.stop()
    count = st.number_input("How many tokens?", 1, 500, 10)
    prefix = st.text_input("Token Prefix", "BYWOB-2025")
    if st.button("Generate Tokens"):
        import random, string
        tokens = []
        for _ in range(count):
            t = prefix + "-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            tokens.append([None,None,t,"FALSE",None])
        sheet.worksheet("voters").append_rows(tokens)
        st.success(f"{count} tokens generated")
