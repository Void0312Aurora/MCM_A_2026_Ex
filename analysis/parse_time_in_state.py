from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path


def parse_time_in_state_text(text: str) -> dict[int, int]:
    """Parse kernel time_in_state content.

    Expected lines: <freq_khz> <time_ms>
    """
    out: dict[int, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            freq = int(parts[0])
            t = int(parts[1])
        except Exception:
            continue
        out[freq] = t
    return out


@dataclass
class ClusterTable:
    freqs_khz: list[int]
    powers_ma: list[float]


def load_cluster_freq_power_csv(path: Path) -> ClusterTable:
    freqs: list[int] = []
    powers: list[float] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            freqs.append(int(row["freq_khz"]))
            powers.append(float(row["power_ma"]))
    if len(freqs) != len(powers) or not freqs:
        raise ValueError(f"Bad cluster csv: {path}")
    return ClusterTable(freqs_khz=freqs, powers_ma=powers)


def estimate_energy_mj(
    delta_time_ms_by_freq: dict[int, int],
    cluster: ClusterTable,
    voltage_mv: int,
    default_power_ma: float | None = None,
) -> tuple[float, float, float]:
    """Estimate energy in mJ.

    - Mapping: exact freq match first; else optionally fallback.
    - Returns: (energy_mJ, matched_energy_mJ, unmatched_energy_mJ)
    """
    freq_to_current_ma = {f: p for f, p in zip(cluster.freqs_khz, cluster.powers_ma)}

    matched = 0.0
    unmatched = 0.0
    for freq, dt_ms in delta_time_ms_by_freq.items():
        cur_ma = freq_to_current_ma.get(freq)
        if cur_ma is None:
            if default_power_ma is None:
                continue
            cur_ma = default_power_ma
            # power_mw = mA * mV / 1000
            power_mw = float(cur_ma) * float(voltage_mv) / 1000.0
            unmatched += power_mw * float(dt_ms)
        else:
            power_mw = float(cur_ma) * float(voltage_mv) / 1000.0
            matched += power_mw * float(dt_ms)

    # mW * ms / 1000 = mJ
    return (matched + unmatched) / 1000.0, matched / 1000.0, unmatched / 1000.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Estimate CPU energy from time_in_state deltas using cluster freq-power table")
    parser.add_argument("--cluster-csv", type=Path, required=True, help="clusterX_freq_power.csv")
    parser.add_argument("--deltas-csv", type=Path, required=True, help="CSV produced by adb_sample_power.py")
    parser.add_argument("--out", type=Path, required=True, help="Output CSV with cpu_energy_mJ column")
    parser.add_argument("--policy", type=int, required=True, help="Policy id mapped to this cluster")
    parser.add_argument(
        "--voltage-col",
        default="battery_voltage_mv",
        help="Column name providing battery voltage in mV (default: battery_voltage_mv)",
    )
    parser.add_argument(
        "--nominal-voltage-mv",
        type=int,
        default=3700,
        help="Fallback nominal voltage if voltage column missing/empty",
    )
    args = parser.parse_args()

    cluster = load_cluster_freq_power_csv(args.cluster_csv)

    with args.deltas_csv.open("r", encoding="utf-8", newline="") as fin, args.out.open(
        "w", encoding="utf-8", newline=""
    ) as fout:
        reader = csv.DictReader(fin)
        fieldnames = list(reader.fieldnames or [])
        if "cpu_energy_mJ" not in fieldnames:
            fieldnames.append("cpu_energy_mJ")
        if "cpu_energy_mJ_matched" not in fieldnames:
            fieldnames.append("cpu_energy_mJ_matched")
        if "cpu_energy_mJ_unmatched" not in fieldnames:
            fieldnames.append("cpu_energy_mJ_unmatched")

        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        prefix = f"cpu_p{args.policy}_freq"
        suffix = "_dt"

        for row in reader:
            delta_map: dict[int, int] = {}
            for k, v in row.items():
                if not k.startswith(prefix) or not k.endswith(suffix):
                    continue
                freq_s = k[len(prefix) : -len(suffix)]
                try:
                    freq = int(freq_s)
                    dt_ms = int(float(v)) if v not in (None, "") else 0
                except Exception:
                    continue
                delta_map[freq] = dt_ms

            voltage_mv = args.nominal_voltage_mv
            if args.voltage_col in row and row[args.voltage_col] not in (None, ""):
                try:
                    voltage_mv = int(float(row[args.voltage_col]))
                except Exception:
                    voltage_mv = args.nominal_voltage_mv

            e, em, eu = estimate_energy_mj(delta_map, cluster, voltage_mv=voltage_mv)
            row["cpu_energy_mJ"] = f"{e:.3f}"
            row["cpu_energy_mJ_matched"] = f"{em:.3f}"
            row["cpu_energy_mJ_unmatched"] = f"{eu:.3f}"
            writer.writerow(row)

    print(f"Wrote: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
