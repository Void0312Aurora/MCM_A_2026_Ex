from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class ClusterTable:
    freqs_khz: list[int]
    current_ma: list[float]


def load_cluster_csv(path: Path) -> ClusterTable:
    freqs: list[int] = []
    current: list[float] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            freqs.append(int(row["freq_khz"]))
            current.append(float(row["power_ma"]))
    if not freqs or len(freqs) != len(current):
        raise ValueError(f"Bad cluster CSV: {path}")
    return ClusterTable(freqs_khz=freqs, current_ma=current)


def load_mapping(path: Path) -> dict[int, int]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    mp = obj.get("mapping_policy_to_cluster") or {}
    out: dict[int, int] = {}
    for k, v in mp.items():
        out[int(k)] = int(v)
    return out


def parse_ts(ts: str) -> datetime | None:
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        # Python 3.11+ supports fromisoformat with timezone offset
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich a run CSV with CPU energy (mJ) using power_profile + policy mapping")
    parser.add_argument("--run-csv", type=Path, required=True)
    parser.add_argument(
        "--map-json",
        type=Path,
        default=Path("artifacts/android/power_profile/policy_cluster_map.json"),
        help="policy_cluster_map.json",
    )
    parser.add_argument(
        "--clusters-dir",
        type=Path,
        default=Path("artifacts/android/power_profile"),
        help="Directory containing clusterX_freq_power.csv",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--voltage-col", default="battery_voltage_mv")
    parser.add_argument("--charge-col", default="charge_counter_uAh")
    args = parser.parse_args()

    mapping = load_mapping(args.map_json)

    # Load cluster tables needed
    cluster_tables: dict[int, ClusterTable] = {}
    for cluster in sorted(set(mapping.values())):
        cluster_tables[cluster] = load_cluster_csv(args.clusters_dir / f"cluster{cluster}_freq_power.csv")

    # Build policy->freq->mA lookups
    policy_lookup: dict[int, dict[int, float]] = {}
    for policy, cluster in mapping.items():
        table = cluster_tables[cluster]
        policy_lookup[policy] = {f: ma for f, ma in zip(table.freqs_khz, table.current_ma)}

    with args.run_csv.open("r", encoding="utf-8", newline="") as fin:
        reader = csv.DictReader(fin)
        in_fields = list(reader.fieldnames or [])

        # Add output fields
        out_fields = list(in_fields)
        for policy in sorted(mapping.keys()):
            for suffix in ["energy_mJ", "energy_mJ_matched", "energy_mJ_unmatched", "avg_power_mW"]:
                col = f"cpu_policy{policy}_{suffix}"
                if col not in out_fields:
                    out_fields.append(col)
        for col in ["cpu_energy_mJ_total", "battery_discharge_energy_mJ", "dt_s"]:
            if col not in out_fields:
                out_fields.append(col)

        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8", newline="") as fout:
            writer = csv.DictWriter(fout, fieldnames=out_fields)
            writer.writeheader()

            prev_ts: datetime | None = None
            prev_charge_uah: int | None = None

            for row in reader:
                # Compute dt_s from timestamps when possible
                ts = parse_ts(row.get("ts_pc", ""))
                dt_s = ""
                if ts and prev_ts:
                    dt = (ts - prev_ts).total_seconds()
                    if dt >= 0:
                        dt_s = f"{dt:.3f}"
                prev_ts = ts or prev_ts

                # Voltage in mV
                voltage_mv = None
                v_raw = row.get(args.voltage_col, "")
                if v_raw not in (None, ""):
                    try:
                        voltage_mv = int(float(v_raw))
                    except Exception:
                        voltage_mv = None

                cpu_total = 0.0

                for policy in sorted(mapping.keys()):
                    lookup = policy_lookup.get(policy, {})

                    matched_mw_ms = 0.0
                    unmatched_mw_ms = 0.0
                    sum_dt_ms = 0

                    prefix = f"cpu_p{policy}_freq"
                    suffix = "_dt"
                    for k, v in row.items():
                        if not k.startswith(prefix) or not k.endswith(suffix):
                            continue
                        freq_s = k[len(prefix) : -len(suffix)]
                        try:
                            freq = int(freq_s)
                            dt_ms = int(float(v)) if v not in (None, "") else 0
                        except Exception:
                            continue
                        if dt_ms <= 0:
                            continue
                        sum_dt_ms += dt_ms

                        cur_ma = lookup.get(freq)
                        if cur_ma is None:
                            # no mapping for this freq
                            continue
                        if voltage_mv is None:
                            continue
                        power_mw = float(cur_ma) * float(voltage_mv) / 1000.0
                        mw_ms = power_mw * float(dt_ms)
                        matched_mw_ms += mw_ms

                    energy_mJ = (matched_mw_ms + unmatched_mw_ms) / 1000.0
                    energy_mJ_matched = matched_mw_ms / 1000.0
                    energy_mJ_unmatched = unmatched_mw_ms / 1000.0
                    cpu_total += energy_mJ

                    row[f"cpu_policy{policy}_energy_mJ"] = f"{energy_mJ:.3f}" if voltage_mv is not None else ""
                    row[f"cpu_policy{policy}_energy_mJ_matched"] = f"{energy_mJ_matched:.3f}" if voltage_mv is not None else ""
                    row[f"cpu_policy{policy}_energy_mJ_unmatched"] = f"{energy_mJ_unmatched:.3f}" if voltage_mv is not None else ""

                    # Average power over the actually-accounted time_in_state window
                    if sum_dt_ms > 0:
                        avg_p = (energy_mJ * 1000.0) / (float(sum_dt_ms) / 1000.0)  # mW
                        row[f"cpu_policy{policy}_avg_power_mW"] = f"{avg_p:.3f}" if voltage_mv is not None else ""
                    else:
                        row[f"cpu_policy{policy}_avg_power_mW"] = ""

                row["cpu_energy_mJ_total"] = f"{cpu_total:.3f}" if voltage_mv is not None else ""
                row["dt_s"] = dt_s

                # Battery discharge energy based on charge_counter (uAh) and voltage (mV)
                discharge_mJ = ""
                ch_raw = row.get(args.charge_col, "")
                if ch_raw not in (None, ""):
                    try:
                        charge_uah = int(float(ch_raw))
                    except Exception:
                        charge_uah = None
                    if charge_uah is not None and prev_charge_uah is not None and voltage_mv is not None:
                        d_uah = charge_uah - prev_charge_uah
                        # discharge: charge counter usually decreases => -d_uah positive
                        discharge_mJ_val = (-float(d_uah)) * float(voltage_mv) * 0.0036
                        discharge_mJ = f"{discharge_mJ_val:.3f}"
                    prev_charge_uah = charge_uah if charge_uah is not None else prev_charge_uah

                row["battery_discharge_energy_mJ"] = discharge_mJ

                writer.writerow({k: row.get(k, "") for k in out_fields})

    print(f"Wrote: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
