#!/usr/bin/env python3
"""
Dynamic Tax-Head Segregation — Streamlit UI
=============================================
Front end for segregate.py's engine. What differs between Pourashavas is
editable here, at runtime, with no code changes:
  - the tax-head catalog (id / name / rate% / add a new head)
  - which of those heads THIS Pourashava has activated (1, 2, 3, 4, or more)
Master-table column names are fixed (Id / AnnualValuation / FinalValuation /
TotalTaxRates) - one system's own schema, not an arbitrary external file.

Run:
    streamlit run app.py
"""
import io
import json
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from segregate import segregate, validate

st.set_page_config(page_title="Tax-Head Segregation", layout="wide")
st.title("Dynamic Tax-Head Segregation")
st.caption(
    "Splits a master holding table's single total tax amount into per-tax-head "
    "line items. Catalog and per-Pourashava activation are editable below."
)

DEFAULT_HEADS = [
    {"TaxHeadId": "625", "Name": "Head 625", "RatePercent": "7", "Effective": True},
    {"TaxHeadId": "627", "Name": "Head 627", "RatePercent": "3", "Effective": True},
    {"TaxHeadId": "628", "Name": "Head 628", "RatePercent": "7", "Effective": True},
]

if "head_inputs" not in st.session_state:
    st.session_state.head_inputs = [dict(item) for item in DEFAULT_HEADS]

# ---------- Section 1: Tax head catalog ----------
st.header("1. Tax-head catalog")
st.caption(
    "Add tax heads directly below. Each row uses a head id, name, rate %, and an Effective checkbox."
)

col_add, col_clear = st.columns([1, 1])
with col_add:
    if st.button("Add tax head", use_container_width=True):
        st.session_state.head_inputs.append({"TaxHeadId": "", "Name": "", "RatePercent": "", "Effective": True})
with col_clear:
    if st.button("Clear all", use_container_width=True):
        st.session_state.head_inputs = []

for idx, row in enumerate(st.session_state.head_inputs):
    c1, c2, c3, c4 = st.columns([1.2, 2.5, 1.2, 1.0])
    with c1:
        head_id_key = f"head_id_{idx}"
        if head_id_key not in st.session_state:
            st.session_state[head_id_key] = row.get("TaxHeadId", "")
        head_id_value = st.text_input("Head ID", key=head_id_key, label_visibility="collapsed")
    with c2:
        name_key = f"name_{idx}"
        if name_key not in st.session_state:
            st.session_state[name_key] = row.get("Name", "")
        name_value = st.text_input("Name", key=name_key, label_visibility="collapsed")
    with c3:
        rate_key = f"rate_{idx}"
        if rate_key not in st.session_state:
            st.session_state[rate_key] = row.get("RatePercent", "")
        rate_value = st.text_input("Rate %", key=rate_key, label_visibility="collapsed")
    with c4:
        effective_key = f"effective_{idx}"
        if effective_key not in st.session_state:
            st.session_state[effective_key] = bool(row.get("Effective", False))
        effective_value = st.checkbox("Effective", key=effective_key, label_visibility="collapsed")

    st.session_state.head_inputs[idx] = {
        "TaxHeadId": head_id_value,
        "Name": name_value,
        "RatePercent": rate_value,
        "Effective": effective_value,
    }

parsed_heads = []
errors = []
for item in st.session_state.head_inputs:
    head_id = str(item.get("TaxHeadId", "")).strip()
    name = str(item.get("Name", "")).strip()
    rate_text = str(item.get("RatePercent", "")).strip()
    effective = bool(item.get("Effective", False))
    if not head_id or not name or not rate_text:
        continue
    try:
        rate_value = float(rate_text)
    except ValueError:
        errors.append(f"Invalid rate '{rate_text}' for head {head_id}")
        continue
    parsed_heads.append({
        "tax_head_id": int(head_id),
        "name": name,
        "rate_percent": rate_value,
        "default_applicable": effective,
    })

if errors:
    st.error("\n".join(errors))

if parsed_heads:
    st.info(
        f"Active for this run: {len(parsed_heads)} head(s), "
        f"total rate {sum(h['rate_percent'] for h in parsed_heads):.2f}%"
    )
else:
    st.warning("Add at least one completed tax head before generating.")

st.divider()

# ---------- Section 2: Upload ----------
# Allow the user to map the master-table columns from the uploaded file.
PK_COL, AV_COL, FV_COL, TRC_COL = "Id", "AnnualValuation", "FinalValuation", "TotalTaxRates"


def pick_default(columns, preferred_values):
    for value in preferred_values:
        if value in columns:
            return value
    return columns[0] if columns else None


st.header("2. Master file")
master_file = st.file_uploader("Master table export (.xlsx)", type=["xlsx"], key="master_upload")

if master_file is not None:
    master_df = pd.read_excel(master_file)
    st.caption(f"{len(master_df)} rows, {len(master_df.columns)} columns detected.")

    column_options = list(master_df.columns)
    if "master_file_name" not in st.session_state or st.session_state.master_file_name != master_file.name:
        st.session_state.master_file_name = master_file.name
        st.session_state.pk_col = pick_default(column_options, [PK_COL, "Id", "HoldingId"])
        st.session_state.annual_col = pick_default(column_options, [AV_COL, "AnnualValue", "Annual", "AnnualValuation"])
        st.session_state.final_col = pick_default(column_options, [FV_COL, "FinalValue", "Final", "FinalValuation"])
        st.session_state.total_rate_col = pick_default(column_options, [TRC_COL, "TotalTaxRate", "TotalTaxRates"]) or ""

    with st.expander("Column mapping", expanded=True):
        pk_col = st.selectbox(
            "Primary key column",
            options=column_options,
            index=column_options.index(st.session_state.pk_col) if st.session_state.pk_col in column_options else 0,
            key="pk_col",
        )
        av_col = st.selectbox(
            "Annual valuation column",
            options=column_options,
            index=column_options.index(st.session_state.annual_col) if st.session_state.annual_col in column_options else 0,
            key="annual_col",
        )
        fv_col = st.selectbox(
            "Final valuation column",
            options=column_options,
            index=column_options.index(st.session_state.final_col) if st.session_state.final_col in column_options else 0,
            key="final_col",
        )
        trc_opts = [""] + column_options
        current_trc = st.session_state.total_rate_col
        trc_idx = trc_opts.index(current_trc) if current_trc in trc_opts else 0
        trc_col = st.selectbox(
            "Total tax rates column (optional)",
            options=trc_opts,
            index=trc_idx,
            key="total_rate_col",
        ) or None

    if not pk_col or not av_col or not fv_col:
        st.error("Please select a primary key and both valuation columns before generating.")
        st.stop()

    st.divider()

    # ---------- Section 3: Generate ----------
    st.header("3. Generate")
    g1, g2 = st.columns(2)
    with g1:
        iuser = st.number_input("IUser (inserting user id)", value=360, step=1)
    with g2:
        idate = st.date_input("IDate", value=date.today()).isoformat()

    if st.button("Generate taxhead rows", type="primary", disabled=not parsed_heads):
        col_map = {
            "master_table": {
                "primary_key": pk_col,
                "AnnualValuation": av_col,
                "annual_valuation": av_col,
                "FinalValuation": fv_col,
                "final_valuation": fv_col,
                "total_rate_check": trc_col,
            },
            "taxhead_table": {
                "holding_id": "HoldingId", "tax_head_id": "TaxHeadId", "rate": "Rate",
                "rate_type": "RateType", "head_tax_annual": "HeadTaxOnAnnual",
                "head_tax_final": "HeadTaxOnFinal", "iuser": "IUser", "idate": "IDate",
                "euser": "EUser", "edate": "EDate",
            },
        }
        out_df, exc_df = segregate(master_df, parsed_heads, col_map, int(iuser), idate)
        st.session_state.out_df = out_df
        st.session_state.exc_df = exc_df
        st.session_state.col_map = col_map

    if "out_df" in st.session_state:
        out_df = st.session_state.out_df
        exc_df = st.session_state.exc_df
        n_holdings = out_df["HoldingId"].nunique() if len(out_df) else 0
        st.success(f"Generated {len(out_df)} rows for {n_holdings} holdings. "
                   f"{len(exc_df)} holding(s) flagged for manual review.")
        st.dataframe(out_df.head(200), use_container_width=True)

        def to_excel_bytes(df):
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df.to_excel(writer, index=False)
            return buf.getvalue()

        d1, d2 = st.columns(2)
        with d1:
            st.download_button("Download generated taxhead.xlsx", data=to_excel_bytes(out_df),
                                file_name="taxhead_generated.xlsx")
        if len(exc_df):
            with d2:
                st.download_button("Download exceptions report", data=to_excel_bytes(exc_df),
                                    file_name="exceptions_report.xlsx")
                st.dataframe(exc_df, use_container_width=True)

        st.divider()

        # ---------- Section 4: Optional validation ----------
        st.header("4. Validate against a known-correct taxhead file (optional)")
        truth_file = st.file_uploader("Reference taxhead.xlsx", type=["xlsx"], key="truth_upload")
        if truth_file is not None:
            truth_path = Path("/tmp/_truth_upload.xlsx")
            truth_path.write_bytes(truth_file.read())
            report, mismatches = validate(out_df, truth_path, st.session_state.col_map)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Rows matched by key", f"{report['matched_keys']}/{report['truth_rows']}")
            m2.metric("Exact value matches", report["exact_value_matches"])
            m3.metric("Accuracy", f"{report['accuracy_pct']}%")
            m4.metric("Missing / extra", f"{report['missing_in_generated']} / {report['extra_in_generated']}")
            if len(mismatches):
                st.caption(f"{len(mismatches)} matched-key rows differ in value:")
                st.dataframe(mismatches, use_container_width=True)
else:
    st.info("Upload a master file to continue.")
