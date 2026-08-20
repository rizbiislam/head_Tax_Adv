import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd


def _get_mapping_value(mapping: dict, *keys, default=None):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _coerce_numeric(value):
    if pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_column(master_df: pd.DataFrame, mapping: dict, *keys, default=None):
    candidates = []
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            candidates.append(value)
    for key in keys:
        if key not in (None, ""):
            candidates.append(key)
    if default not in (None, ""):
        candidates.append(default)

    alias_map = {
        "AnnualValuation": ["AnnualValuation", "AnnualValue", "Annual", "Annual Valuation", "Annual_Valuation"],
        "annual_valuation": ["AnnualValuation", "AnnualValue", "Annual", "Annual Valuation", "Annual_Valuation"],
        "FinalValuation": ["FinalValuation", "FinalValue", "Final", "Final Valuation", "Final_Valuation"],
        "final_valuation": ["FinalValuation", "FinalValue", "Final", "Final Valuation", "Final_Valuation"],
        "TotalTaxRates": ["TotalTaxRates", "TotalTaxRate", "TotalTax"],
        "total_rate_check": ["TotalTaxRates", "TotalTaxRate", "TotalTax"],
    }

    for candidate in candidates:
        if candidate in master_df.columns:
            return candidate
        for alias in alias_map.get(candidate, []):
            if alias in master_df.columns:
                return alias

    for candidate in candidates:
        if candidate not in (None, ""):
            return candidate
    return default


def load_config(config_dir: Path):
    rate_master = json.loads((config_dir / "rate_master.json").read_text(encoding="utf-8"))
    col_map = json.loads((config_dir / "column_mapping.json").read_text(encoding="utf-8"))
    heads = [h for h in rate_master["tax_heads"] if h.get("tax_head_id") is not None]
    if not heads:
        raise ValueError("rate_master.json has no usable tax heads (all placeholders?)")
    # Optional per-holding overrides: map of HoldingId -> list of tax_head_id
    overrides_path = config_dir / "overrides.json"
    overrides = {}
    if overrides_path.exists():
        try:
            overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        except Exception:
            # ignore malformed overrides and keep empty
            overrides = {}
    return heads, col_map, overrides


def segregate(master_df: pd.DataFrame, heads: list, col_map: dict,
              iuser: int, idate: str, overrides: dict = None):
    m = col_map["master_table"]
    t = col_map["taxhead_table"]

    primary_key_col = _resolve_column(master_df, m, "primary_key", "PrimaryKey", default="Id")
    annual_col = _resolve_column(master_df, m, "AnnualValuation", "annual_valuation", "AnnualValuationuation", "AnnualValue", default="AnnualValuation")
    final_col = _resolve_column(master_df, m, "FinalValuation", "final_valuation", "FinalValuationuation", "FinalValue", default="FinalValuation")
    total_rate_col = _resolve_column(master_df, m, "total_rate_check", "TotalTaxRates", "TotalTaxRate", default="TotalTaxRates")

    default_heads = [h for h in heads if h["default_applicable"]]
    default_total_rate = round(sum(h["rate_percent"] for h in default_heads), 6)
    has_total_check_col = bool(total_rate_col and total_rate_col in master_df.columns)

    missing_columns = [col for col in [primary_key_col, annual_col] if col and col not in master_df.columns]
    if missing_columns:
        raise KeyError(f"Required master columns not found: {', '.join(missing_columns)}")

    rows = []
    exceptions = []

    for _, r in master_df.iterrows():
        holding_id = r[primary_key_col]
        annual_valuation = _coerce_numeric(r[annual_col]) if annual_col else None
        final_valuation = _coerce_numeric(r[final_col]) if final_col and final_col in master_df.columns else annual_valuation
        total_check = _coerce_numeric(r[total_rate_col]) if has_total_check_col else None

        # Skip condition: driven by the source's own "no tax applies" marker
        # (TotalTaxRates == 0), NOT by AnnualValuation == 0 - confirmed 1:1
        # against every holding with zero taxhead rows in the sample data.
        # A holding can have AnnualValuation == 0 and still need an explicit
        # $0 row if TotalTaxRates says tax heads apply.
        if has_total_check_col:
            if total_check is None or total_check == 0:
                continue
        elif annual_valuation is None or annual_valuation == 0:
            # No total-rate marker available in this source schema - fall
            # back to valuation as the best available signal.
            continue

        if total_check is not None and total_check != 0:
            # Dynamic match: walk `heads` in the order they're listed,
            # accumulating rate_percent, until the running total reaches
            # THIS holding's own total_check. Works for any client's rate
            # combination without specifying anything per client - the
            # order of `heads` is the only configuration needed, once.
            running_total = 0.0
            matched_heads = []
            for h in heads:
                if running_total >= total_check - 1e-6:
                    break
                matched_heads.append(h)
                running_total += float(h.get("rate_percent", 0) or 0)

            if abs(running_total - total_check) <= 1e-6:
                for h in matched_heads:
                    rate_percent = float(h.get("rate_percent", 0) or 0)
                    rows.append({
                        t["holding_id"]: holding_id,
                        t["tax_head_id"]: h["tax_head_id"],
                        t["rate"]: rate_percent,
                        t["rate_type"]: 1,
                        t["head_tax_annual"]: round(annual_valuation * rate_percent / 100) if annual_valuation is not None else 0,
                        t["head_tax_final"]: round(final_valuation * rate_percent / 100) if final_valuation is not None else 0,
                        t["iuser"]: iuser,
                        t["idate"]: idate,
                        t["euser"]: None,
                        t["edate"]: None,
                    })
                continue

            # Serial match couldn't reach this holding's total rate exactly -
            # fall back to an explicit override if one exists for it, else
            # flag it. Guessing would mean writing a wrong tax amount.
            used_heads = None
            if overrides:
                # support numeric keys and string keys
                key_candidates = [holding_id, str(holding_id)]
                for k in key_candidates:
                    if k in overrides:
                        override_ids = overrides[k] or []
                        # pick heads that match these ids
                        used_heads = [h for h in heads if h.get("tax_head_id") in override_ids]
                        break

            if used_heads is None:
                exceptions.append({
                    "HoldingId": holding_id,
                    "Issue": "No combination of the catalog, consumed in order, reaches this holding's total rate - nothing generated, needs manual review",
                    "SourceTotalRate": total_check,
                    "ReachedRate": running_total,
                })
                continue
            else:
                # If we found an override head set, generate rows from it
                for h in used_heads:
                    rate_percent = float(h.get("rate_percent", 0) or 0)
                    rows.append({
                        t["holding_id"]: holding_id,
                        t["tax_head_id"]: h["tax_head_id"],
                        t["rate"]: rate_percent,
                        t["rate_type"]: 1,
                        t["head_tax_annual"]: round(annual_valuation * rate_percent / 100) if annual_valuation is not None else 0,
                        t["head_tax_final"]: round(final_valuation * rate_percent / 100) if final_valuation is not None else 0,
                        t["iuser"]: iuser,
                        t["idate"]: idate,
                        t["euser"]: None,
                        t["edate"]: None,
                    })
                # continue to next holding since we've generated rows
                continue

        for h in default_heads:
            rate_percent = float(h.get("rate_percent", 0) or 0)
            rows.append({
                t["holding_id"]: holding_id,
                t["tax_head_id"]: h["tax_head_id"],
                t["rate"]: rate_percent,
                t["rate_type"]: 1,
                t["head_tax_annual"]: round(annual_valuation * rate_percent / 100) if annual_valuation is not None else 0,
                t["head_tax_final"]: round(final_valuation * rate_percent / 100) if final_valuation is not None else 0,
                t["iuser"]: iuser,
                t["idate"]: idate,
                t["euser"]: None,
                t["edate"]: None,
            })

    out_cols = [t["holding_id"], t["tax_head_id"], t["rate"], t["rate_type"],
                t["head_tax_annual"], t["head_tax_final"], t["iuser"], t["idate"],
                t["euser"], t["edate"]]
    out_df = pd.DataFrame(rows, columns=out_cols) if rows else pd.DataFrame(columns=out_cols)
    exc_df = pd.DataFrame(exceptions)
    return out_df, exc_df


def validate(generated_df: pd.DataFrame, truth_path: Path, col_map: dict):
    t = col_map["taxhead_table"]
    truth_df = pd.read_excel(truth_path)

    gen_key = generated_df[[t["holding_id"], t["tax_head_id"], t["head_tax_annual"]]].copy()
    gen_key.columns = ["HoldingId", "TaxHeadId", "Generated"]
    truth_key = truth_df[[t["holding_id"], t["tax_head_id"], t["head_tax_annual"]]].copy()
    truth_key.columns = ["HoldingId", "TaxHeadId", "Actual"]

    merged = truth_key.merge(gen_key, on=["HoldingId", "TaxHeadId"], how="outer", indicator=True)
    merged["Match"] = merged["Generated"] == merged["Actual"]

    report = {
        "truth_rows": len(truth_df),
        "generated_rows": len(generated_df),
        "matched_keys": int((merged["_merge"] == "both").sum()),
        "exact_value_matches": int(merged["Match"].sum()),
        "missing_in_generated": int((merged["_merge"] == "left_only").sum()),
        "extra_in_generated": int((merged["_merge"] == "right_only").sum()),
        "accuracy_pct": round(100 * merged["Match"].sum() / len(truth_df), 3) if len(truth_df) else None,
    }
    mismatches = merged[(merged["_merge"] == "both") & (~merged["Match"])]
    return report, mismatches


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", required=True, help="Path to master table Excel export")
    ap.add_argument("--out", required=True, help="Path to write generated taxhead Excel")
    ap.add_argument("--config-dir", default=str(Path(__file__).parent / "config"))
    ap.add_argument("--iuser", type=int, default=360, help="Inserting user id to stamp on generated rows")
    ap.add_argument("--idate", default=date.today().isoformat(), help="Insert date to stamp (YYYY-MM-DD)")
    ap.add_argument("--validate-against", help="Path to a known-correct taxhead Excel, to score accuracy")
    ap.add_argument("--exceptions-out", help="Path to write the exceptions/manual-review report")
    args = ap.parse_args()

    heads, col_map, overrides = load_config(Path(args.config_dir))
    master_df = pd.read_excel(args.master)

    out_df, exc_df = segregate(master_df, heads, col_map, args.iuser, args.idate, overrides)
    out_df.to_excel(args.out, index=False)
    print(f"Generated {len(out_df)} tax-head rows for {out_df[col_map['taxhead_table']['holding_id']].nunique()} holdings -> {args.out}")
    print(f"Exceptions flagged for manual review: {len(exc_df)}")

    if args.exceptions_out and len(exc_df):
        exc_df.to_excel(args.exceptions_out, index=False)
        print(f"Exceptions written -> {args.exceptions_out}")

    if args.validate_against:
        report, mismatches = validate(out_df, Path(args.validate_against), col_map)
        print("\n--- Validation against", args.validate_against, "---")
        for k, v in report.items():
            print(f"  {k}: {v}")
        if len(mismatches):
            mism_path = str(Path(args.out).with_suffix("")) + "_mismatches.xlsx"
            mismatches.to_excel(mism_path, index=False)
            print(f"  mismatches written -> {mism_path}")


if __name__ == "__main__":
    sys.exit(main())
