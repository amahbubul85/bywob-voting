# streamlit_app.py
# BYWOB Online Voting — Streamlit + Google Sheets
# Features:
# - One-time token voting
# - Election window (start/end in UTC): idle | ongoing | ended | published
# - Blocks voting outside window, publish/declare results
# - Token generator (no hard max)
# - Live tally, CSV export
# - Archive & clear votes for next election
# - Robust 'used' handling (string/boolean)

import streamlit as st
import pandas as pd
from datetime import datetime, date, time as dtime, timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️", layout="centered")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + Google Sheets • Secret ballot with one-time tokens")

# --------------------------------------------------------------------------------------
# Secrets & Google Sheets connection
# --------------------------------------------------------------------------------------
def _require_secrets():
    if "gcp_service_account" not in st.secrets:
        st.error(
            "Secrets missing: gcp_service_account. "
            "App → Settings → Secrets এ আপনার service account JSON এবং SHEET_ID বসান।"
        )
        st.stop()

_require_secrets()

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
client = gspread.authorize(creds)

SHEET_ID = st.secrets["gcp_service_account"]["SHEET_ID"]
sheet = client.ope
