import streamlit as st
import pandas as pd
from datetime import datetime
import os

st.set_page_config(page_title="BYWOB Online Voting", page_icon="🗳️")
st.title("🗳️ BYWOB Online Voting")
st.caption("Streamlit Cloud + Google Sheets • Secret ballot with one-time tokens")

# ===== 0) Quick self-test & diagnostics =====
def diag_fail(msg, exc=None):
    st.error(msg)
    if exc:
        st.exception(exc)
    st.stop()

# A. check requirements loaded
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
except Exception as e:
    diag_fail("⚠️ gspread/oauth2client লোড হতে পারেনি। requirements.txt এ `gspread` ও `oauth2client` আছে কি?", e)

# B. check secrets
if "gcp_service_account" not in st.secrets:
    diag_fail("⚠️ Secrets এ `[gcp_service_account]` ব্লক নেই। Streamlit Cloud → Settings → Secrets এ JSON ব্লক দিন।")

svc = st.secrets["gcp_service_account"]
required_keys = ["type","project_id","private_key_id","private_key","client_email","client_id","token_uri","SHEET_ID"]
missing = [k for k in required_keys if k not in svc or str(svc[k]).strip()==""]
if missing:
    diag_fail("⚠️ Secrets এ নিচের কী গুলো নেই/ফাঁকা: " + ", ".join(missing))

# C. connect to Google Sheets
try:
    scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(svc, scope)
    client = gspread.authorize(creds)
except Exception as e:
    diag_fail("⚠️ Service account দিয়ে অথোরাইজ করা যাচ্ছে না। JSON ঠিক আছে তো? `private_key`-এ \\n লাইনব্রেক ঠিক আছে?", e)

# D. open sheet
SHEET_ID = svc["SHEET_ID"]
try:
    sheet = client.open_by_key(SHEET_ID)
except Exception as e:
    diag_fail("⚠️ Google Sheet ওপেন করা যাচ্ছে না। SHEET_ID ঠিক আছে? আর শিটটি service account ইমেইলকে Editor দিয়ে শেয়ার করা আছে?", e)

# E. open worksheets
def open_ws(name):
    try:
        return sheet.worksheet(name)
    except Exception as e:
        diag_fail(f"⚠️ `{name}` নামের worksheet পাওয়া যায়নি। শিটে ঠিক এই নামে ট্যাব আছে তো?", e)

voters_ws     = open_ws("voters")
candidates_ws = open_ws("candidates")
votes_ws      = open_ws("votes")

st.success("✅ Google Sheets কানেক্টেড (voters/candidates/votes পাওয়া গেছে)")

ADMIN_PASSWORD = st.secrets.get("ADMIN_PASSWORD", None)
PLACEHOLDER = "— একজন প্রার্থী নির্বাচন করুন —"

# ===== 1) Data loaders (with cleaning) =====
@st.cache_data
def load_voters():
    df = pd.DataFrame(voters_ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["name","email","token","used","used_at"])
    return df

@st.cache_data
def load_candidates():
    df = pd.DataFrame(candidates_ws.get_all_records())
    if df.empty:
        return pd.DataFrame(columns=["position","candidate"])
    df["position"]  = df["position"].astype(str).strip()
    df["candidate"] = df["candidate"].astype(str).strip()
    df = df[(df["position"]!="") & (df["candidate"]!="")]
    return df

def _boolish(x): return str(x).strip().lower() in ["true","1","yes"]

def validate_token(voters, token):
    row = voters.loc[voters["token"] == token]
    if row.empty: return None, "❌ টোকেন সঠিক নয়।"
    if _boolish(row.iloc[0]["used"]): return None, "⚠️ এই টোকেনটি ইতিমধ্যে ব্যবহার করা হয়েছে।"
    return row.iloc[0], None

def mark_token_used(token):
    voters = load_voters()
    idx = voters[voters["token"] == token].index
    if len(idx)==0: return
    r = idx[0]+2
    voters_ws.update_cell(r, voters.columns.get_loc("used")+1, True)
    voters_ws.update_cell(r, voters.columns.get_loc("used_at")+1, datetime.utcnow().isoformat())
    load_voters.clear()

def save_vote_batch(selections, positions):
    now = datetime.utcnow().isoformat()
    rows = [[pos, selections[pos], now] for pos in positions]
    votes_ws.append_rows(rows, value_input_option="RAW")

def ballot_form(cands, positions):
    selections = {}
    with st.form("ballot_form"):
        st.info("প্রতিটি পদের জন্য একজন প্রার্থী নির্বাচন করুন, তারপর Submit ক্লিক করুন।")
        for pos in positions:
            opts = [PLACEHOLDER] + cands[cands["position"]==pos]["candidate"].tolist()
            choice = st.radio(f"**{pos}**", opts, index=0, key=f"radio_{pos}")
            selections[pos] = None if choice==PLACEHOLDER else choice
        missing = [p for p in positions if selections[p] is None]
        submitted = st.form_submit_button("✅ ভোট জমা দিন", disabled=(len(missing)!=0))
        return submitted, selections, missing

def tally(cands):
    votes = pd.DataFrame(votes_ws.get_all_records())
    if votes.empty:
        st.info("এখনও কোনো ভোট পড়েনি।")
        return
    st.subheader("📊 Live Tally")
    for pos in cands["position"].unique():
        st.markdown(f"**{pos}**")
        counts = votes[votes["position"]==pos]["candidate"].value_counts()
        st.table(counts.rename("votes"))

# ===== 2) Main UI =====
voters = load_voters()
cands  = load_candidates()

if cands.empty:
    st.warning("`candidates` শিট খালি। অন্তত একটি পদের জন্য প্রার্থী যুক্ত করুন।")
positions = sorted(pd.unique(cands["position"]))

tab_vote, tab_admin = st.tabs(["🔐 Vote", "🛠️ Admin"])

with tab_vote:
    token = st.text_input("আপনার ওয়ান-টাইম টোকেন লিখুন", type="password")
    if token:
        rec, err = validate_token(voters, token.strip())
        if err: st.error(err)
        else:
            st.success(f"স্বাগতম, {rec['name']} — সব পদের জন্য ভোট দিন।")
            submitted, selections, missing = ballot_form(cands, positions)
            if submitted:
                if len(missing)==0:
                    mark_token_used(token.strip())
                    save_vote_batch(selections, positions)
                    st.success("✅ আপনার ভোট সংরক্ষিত হয়েছে। ধন্যবাদ!")
                else:
                    st.error("❗ এই পজিশনগুলোতে নির্বাচন বাকি: " + ", ".join(missing))

with tab_admin:
    st.caption("Admin (ADMIN_PASSWORD সেট করলে পাসওয়ার্ড লাগবে)")
    ok = True
    if ADMIN_PASSWORD:
        pw = st.text_input("Admin password", type="password")
        ok = (pw == ADMIN_PASSWORD)
        if pw and not ok: st.error("Wrong password")
    if ok:
        with st.expander("📋 Candidates"): st.dataframe(cands)
        with st.expander("🧑‍🤝‍🧑 Voters (tokens hidden)"):
            st.dataframe(voters.assign(token="••••••••") if not voters.empty else voters)
        with st.expander("📊 Live Tally"): tally(cands)
    else:
        st.warning("Please enter admin password.")
