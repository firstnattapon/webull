"""Streamlit dashboard — read-only view of Shannon Demon Firestore state/trades.

Run locally:
    streamlit run dashboard.py

Requires a service account key in `.streamlit/secrets.toml` under the
`[firebase_service_account]` table (see `.streamlit/secrets.toml.example`).
Never commit `.streamlit/secrets.toml` — it holds a live credential.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from google.cloud import firestore
from google.oauth2 import service_account

st.set_page_config(page_title="Shannon Demon Dashboard", layout="wide")


@st.cache_resource
def get_client(project_id: str) -> firestore.Client:
    info = dict(st.secrets["firebase_service_account"])
    creds = service_account.Credentials.from_service_account_info(info)
    return firestore.Client(credentials=creds, project=project_id)


def load_state(db: firestore.Client, collection: str, document: str) -> dict:
    snapshot = db.collection(collection).document(document).get()
    return snapshot.to_dict() or {}


def load_trades(db: firestore.Client, collection: str, limit: int) -> pd.DataFrame:
    docs = (
        db.collection(collection)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    rows = [doc.to_dict() for doc in docs]
    if not rows:
        return pd.DataFrame()
    return pd.json_normalize(rows, sep="_")


with st.sidebar:
    st.header("Firestore target")
    default_project = dict(st.secrets["firebase_service_account"]).get("project_id", "")
    project_id = st.text_input("Project ID", value=default_project)
    st.caption(
        "ใช้ key จาก project ไหน ก็ยิงไป project นั้นได้เฉพาะถ้า service account "
        "มีสิทธิ์ (roles/datastore.viewer) บน project ปลายทางด้วย ถ้า bot เขียนข้อมูล "
        "อยู่คนละ project กับ key นี้ ต้องไปเพิ่ม IAM binding ให้ก่อน"
    )
    state_collection = st.text_input("State collection", value="shannon_demon_state")
    state_document = st.text_input("State document", value="SHANNON_DEMON_DNA_SMR")
    trade_collection = st.text_input("Trade collection", value="shannon_demon_trades")
    trade_limit = st.number_input("Trades to show", min_value=10, max_value=1000, value=100, step=10)
    if st.button("Refresh"):
        st.cache_resource.clear()
        st.rerun()

if not project_id:
    st.stop()

db = get_client(project_id)

st.title("Shannon Demon Dashboard")

state = load_state(db, state_collection, state_document)
if state:
    cols = st.columns(4)
    cols[0].metric("DNA step", state.get("dna_step", "-"))
    cols[1].metric("Last signal", state.get("last_signal", "-"))
    cols[2].metric("Last status", state.get("last_status", "-"))
    last_logged = state.get("last_logged_at")
    cols[3].metric("Last logged at", str(last_logged) if last_logged else "-")
else:
    st.info(f"ยังไม่มี state document ที่ {state_collection}/{state_document}")

st.subheader("Trade log")
trades = load_trades(db, trade_collection, int(trade_limit))
if trades.empty:
    st.info(f"ยังไม่มี trade log ใน collection {trade_collection}")
else:
    if "status" in trades:
        st.bar_chart(trades["status"].value_counts())
    if "decision_baseline_pnl" in trades and "created_at" in trades:
        pnl_series = trades.set_index("created_at")["decision_baseline_pnl"].sort_index()
        st.line_chart(pnl_series)
    st.dataframe(trades, use_container_width=True)
