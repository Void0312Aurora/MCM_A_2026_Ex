from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd


# -----------------------------
# Power profile parsing
# -----------------------------


@dataclass
class PowerProfile:
    clusters_cores: list[int]
    core_speeds_khz: dict[int, list[int]]
    core_power_ma: dict[int, list[float]]
    items_ma: dict[str, float]
    battery_capacity_mah: int | None


_RE_VALUE = re.compile(r"\n\s*E: value .*?\n\s*T: '(?P<val>[^']*)'", re.MULTILINE)


def _extract_item(xmltree_text: str, key: str) -> str | None:
    idx = xmltree_text.find(f'A: name="{key}"')
    if idx < 0:
        return None

    window = xmltree_text[idx:]
    next_item = window.find("\n    E: item", 1)
    next_array = window.find("\n    E: array", 1)
    stops = [x for x in [next_item, next_array] if x and x > 0]
    if stops:
        window = window[: min(stops)]

    m = re.search(r"\n\s*T: '(?P<val>[^']*)'", window)
    if not m:
        return None
    return m.group("val")


def _extract_array(xmltree_text: str, key: str) -> list[str] | None:
    idx = xmltree_text.find(f'A: name="{key}"')
    if idx < 0:
        return None

    window = xmltree_text[idx:]
    next_idx = window.find("\n    E: array", 1)
    if next_idx > 0:
        window = window[:next_idx]

    return [m.group("val") for m in _RE_VALUE.finditer(window)]


def parse_power_profile_xmltree(xmltree_path: Path) -> PowerProfile:
    text = xmltree_path.read_text(encoding="utf-8", errors="replace")

    clusters_cores: list[int] = []
    cores_vals = _extract_array(text, "cpu.clusters.cores")
    if cores_vals:
        clusters_cores = [int(float(v)) for v in cores_vals]

    core_speeds_khz: dict[int, list[int]] = {}
    core_power_ma: dict[int, list[float]] = {}

    for cluster in range(0, 8):
        speeds = _extract_array(text, f"cpu.core_speeds.cluster{cluster}")
        power = _extract_array(text, f"cpu.core_power.cluster{cluster}")
        if speeds:
            core_speeds_khz[cluster] = [int(float(v)) for v in speeds]
        if power:
            core_power_ma[cluster] = [float(v) for v in power]

    cap = None
    cap_raw = _extract_item(text, "battery.capacity")
    if cap_raw is not None:
        try:
            cap = int(float(cap_raw))
        except Exception:
            cap = None

    item_keys = [
        "screen.on",
        "screen.full",
        "screen.idle",
        "wifi.on",
        "wifi.active",
        "wifi.scan",
        "radio.active",
        "radio.scanning",
        "bluetooth.on",
        "bluetooth.active",
        "gps.on",
        "camera.avg",
        "camera.flashlight",
    ]

    items_ma: dict[str, float] = {}
    for k in item_keys:
        v = _extract_item(text, k)
        if v is None:
            continue
        try:
            items_ma[k] = float(v)
        except Exception:
            continue

    return PowerProfile(
        clusters_cores=clusters_cores,
        core_speeds_khz=core_speeds_khz,
        core_power_ma=core_power_ma,
        items_ma=items_ma,
        battery_capacity_mah=cap,
    )


def write_power_profile_outputs(profile: PowerProfile, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "power_profile.json").write_text(
        json.dumps(
            {
                "battery_capacity_mah": profile.battery_capacity_mah,
                "clusters_cores": profile.clusters_cores,
                "core_speeds_khz": {str(k): v for k, v in profile.core_speeds_khz.items()},
                "core_power_ma": {str(k): v for k, v in profile.core_power_ma.items()},
                "items_ma": {str(k): float(v) for k, v in sorted(profile.items_ma.items())},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    for cluster, speeds in profile.core_speeds_khz.items():
        powers = profile.core_power_ma.get(cluster, [])
        rows = ["freq_khz,power_ma"]
        for i, f in enumerate(speeds):
            p = powers[i] if i < len(powers) else ""
            rows.append(f"{f},{p}")
        (out_dir / f"cluster{cluster}_freq_power.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


# -----------------------------
# Perfetto parses
# -----------------------------


@dataclass(frozen=True)
class BatteryCounterSummary:
    label: str
    trace_path: str
    n_samples: int
    duration_s: float
    sample_period_s_median: float

    charge_start_uah: float | None
    charge_end_uah: float | None
    discharge_mah: float | None

    current_ua_mean: float | None
    current_ua_p50: float | None
    current_ua_p95: float | None

    voltage_v_mean: float | None
    voltage_v_p50: float | None

    power_mw_mean: float | None
    energy_mwh: float | None


def parse_perfetto_android_power_counters(
    trace: Path,
    out_dir: Path | None = None,
    label: str = "",
    no_timeseries: bool = False,
) -> BatteryCounterSummary:
    import numpy as np
    from perfetto.trace_processor import TraceProcessor

    def infer_voltage_scale(voltage_raw: pd.Series) -> float:
        v = pd.to_numeric(voltage_raw, errors="coerce")
        med = float(v.dropna().median()) if v.notna().any() else float("nan")
        if np.isfinite(med) and med < 100_000:
            return 1e-3
        return 1e-6

    def load_batt_counters(tp: TraceProcessor) -> pd.DataFrame:
        df = tp.query(
            """
            select
              c.ts as ts,
              ct.name as name,
              c.value as value
            from counter c
            join counter_track ct on ct.id = c.track_id
            where ct.name glob 'batt.*'
            order by c.ts
            """
        ).as_pandas_dataframe()

        if df.empty:
            return df

        def last(series: pd.Series) -> float:
            s = pd.to_numeric(series, errors="coerce").dropna()
            return float(s.iloc[-1])

        piv = df.pivot_table(index="ts", columns="name", values="value", aggfunc=last).reset_index()
        piv = piv.sort_values("ts").reset_index(drop=True)

        t0 = int(piv["ts"].iloc[0])
        piv.insert(1, "t_s", (piv["ts"] - t0) / 1e9)
        return piv

    if not trace.exists():
        raise FileNotFoundError(f"Trace not found: {trace}")

    out_dir = out_dir or trace.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    label = label or trace.stem

    with TraceProcessor(trace=str(trace)) as tp:
        ts = load_batt_counters(tp)

    if ts.empty:
        raise RuntimeError("No batt.* counter tracks found in trace.")

    charge = ts.get("batt.charge_uah")
    current = ts.get("batt.current_ua")
    voltage_raw = ts.get("batt.voltage_uv")

    voltage_v = None
    if voltage_raw is not None:
        voltage_to_v = infer_voltage_scale(voltage_raw)
        voltage_v = pd.to_numeric(voltage_raw, errors="coerce") * float(voltage_to_v)

    power_mw = None
    energy_mwh = None
    if current is not None and voltage_v is not None:
        i_a = pd.to_numeric(current, errors="coerce") * 1e-6
        p_w = i_a * voltage_v
        power_mw = p_w * 1e3
        ts["power_mw_calc"] = power_mw

        t = pd.to_numeric(ts["t_s"], errors="coerce")
        p = pd.to_numeric(power_mw, errors="coerce")
        if len(t) >= 2:
            dt = t.to_numpy()[1:] - t.to_numpy()[:-1]
            p_avg = (p.to_numpy()[1:] + p.to_numpy()[:-1]) / 2.0
            energy_mwh = float(np.nansum(p_avg * dt) / 3600.0)

    duration_s = float(ts["t_s"].iloc[-1] - ts["t_s"].iloc[0]) if len(ts) else 0.0
    dt_med = float(pd.to_numeric(ts["t_s"], errors="coerce").diff().median()) if len(ts) >= 2 else float("nan")

    discharge_mah = None
    charge_start = float(charge.iloc[0]) if charge is not None and pd.notna(charge.iloc[0]) else None
    charge_end = float(charge.iloc[-1]) if charge is not None and pd.notna(charge.iloc[-1]) else None
    if charge_start is not None and charge_end is not None:
        discharge_mah = float((charge_start - charge_end) / 1000.0)

    def quantile(series: pd.Series | None, q: float) -> float | None:
        if series is None:
            return None
        s = pd.to_numeric(series, errors="coerce").dropna()
        if s.empty:
            return None
        return float(s.quantile(q))

    cur_mean = float(pd.to_numeric(current, errors="coerce").mean()) if current is not None else None
    v_mean = float(pd.to_numeric(voltage_v, errors="coerce").mean()) if voltage_v is not None else None
    p_mean = float(pd.to_numeric(power_mw, errors="coerce").mean()) if power_mw is not None else None

    summary = BatteryCounterSummary(
        label=label,
        trace_path=str(trace),
        n_samples=int(len(ts)),
        duration_s=duration_s,
        sample_period_s_median=dt_med,
        charge_start_uah=charge_start,
        charge_end_uah=charge_end,
        discharge_mah=discharge_mah,
        current_ua_mean=cur_mean,
        current_ua_p50=quantile(current, 0.50),
        current_ua_p95=quantile(current, 0.95),
        voltage_v_mean=v_mean,
        voltage_v_p50=quantile(voltage_v, 0.50) if voltage_v is not None else None,
        power_mw_mean=p_mean,
        energy_mwh=energy_mwh,
    )

    out_json = out_dir / "perfetto_android_power_summary.json"
    out_csv = out_dir / "perfetto_android_power_summary.csv"
    out_json.write_text(json.dumps(asdict(summary), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pd.DataFrame([asdict(summary)]).to_csv(out_csv, index=False, encoding="utf-8")

    if not no_timeseries:
        out_ts = out_dir / "perfetto_android_power_timeseries.csv"
        ts.to_csv(out_ts, index=False, encoding="utf-8")

    return summary


@dataclass(frozen=True)
class PolicyMarkersSummary:
    trace_path: str
    out_dir: str
    keywords: list[str]
    n_markers: int
    notes: list[str]


def parse_perfetto_policy_markers(
    trace: Path,
    out_dir: Path | None = None,
    keywords: list[str] | None = None,
    max_rows: int = 20000,
) -> PolicyMarkersSummary:
    from perfetto.trace_processor import TraceProcessor

    if not trace.exists():
        raise FileNotFoundError(f"Trace not found: {trace}")

    out_dir = out_dir or trace.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    keywords = keywords or [
        "power",
        "PowerHAL",
        "powerhal",
        "boost",
        "hint",
        "mtk",
        "mi",
        "fpsgo",
        "uclamp",
        "cpuset",
        "thermal",
        "throttle",
    ]

    notes: list[str] = []
    markers = pd.DataFrame()

    with TraceProcessor(trace=str(trace)) as tp:
        cols = tp.query("pragma table_info(slice)").as_pandas_dataframe()
        colnames = set(str(x) for x in cols["name"].tolist()) if not cols.empty and "name" in cols.columns else set()
        cat_col = "category" if "category" in colnames else ("cat" if "cat" in colnames else None)

        like_parts: list[str] = []
        for k in keywords:
            k_esc = k.replace("'", "''")
            like_parts.append(f"s.name like '%{k_esc}%'")
            if cat_col:
                like_parts.append(f"s.{cat_col} like '%{k_esc}%'")
        where = " or ".join(like_parts) if like_parts else "1=0"

        try:
            q = f"""
            select
              s.ts as ts,
              s.dur as dur,
              s.name as name,
              {('s.' + cat_col) if cat_col else "'' as category"} as category,
              p.name as process,
              t.name as thread
            from slice s
            join track tr on tr.id = s.track_id
            left join thread_track tt on tt.id = tr.id
            left join thread t on t.utid = tt.utid
            left join process p on p.upid = t.upid
            where s.name is not null and ({where})
            order by s.ts
            limit {int(max_rows)}
            """
            markers = tp.query(q).as_pandas_dataframe()
        except Exception as e:
            notes.append(f"join query failed; falling back to slice-only query: {type(e).__name__}: {e}")
            try:
                q2 = f"""
                select
                  s.ts as ts,
                  s.dur as dur,
                  s.name as name,
                  {('s.' + cat_col) if cat_col else "'' as category"} as category,
                  s.track_id as track_id
                from slice s
                where s.name is not null and ({where})
                order by s.ts
                limit {int(max_rows)}
                """
                markers = tp.query(q2).as_pandas_dataframe()
            except Exception as e2:
                notes.append(f"slice-only query failed: {type(e2).__name__}: {e2}")
                markers = pd.DataFrame()

    if not markers.empty and "ts" in markers.columns:
        t0 = int(pd.to_numeric(markers["ts"], errors="coerce").dropna().iloc[0])
        markers.insert(0, "t_s", (pd.to_numeric(markers["ts"], errors="coerce") - t0) / 1e9)
        if "dur" in markers.columns:
            markers["dur_s"] = pd.to_numeric(markers["dur"], errors="coerce") / 1e9

    out_csv = out_dir / "perfetto_policy_markers.csv"
    out_json = out_dir / "perfetto_policy_markers_summary.json"

    out_dir.mkdir(parents=True, exist_ok=True)
    markers.to_csv(out_csv, index=False, encoding="utf-8")

    summary = PolicyMarkersSummary(
        trace_path=str(trace),
        out_dir=str(out_dir),
        keywords=keywords,
        n_markers=int(len(markers)),
        notes=notes,
    )
    out_json.write_text(json.dumps(asdict(summary), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


# -----------------------------
# Batterystats proto (schema-min)
# -----------------------------


PROTO_PATH = Path(__file__).parent / "proto" / "android" / "os" / "batterystats_min.proto"
GEN_DIR = Path(__file__).parent / "_generated"
PROTO_ROOT = Path(__file__).parent / "proto"


def _ensure_generated() -> None:
    target = GEN_DIR / "android" / "os" / "batterystats_min_pb2.py"
    if target.exists() and target.stat().st_mtime >= PROTO_PATH.stat().st_mtime:
        return

    try:
        from grpc_tools import protoc
    except Exception as e:
        raise RuntimeError("grpcio-tools is required to generate proto bindings") from e

    (GEN_DIR / "android" / "os").mkdir(parents=True, exist_ok=True)
    for p in [GEN_DIR / "android" / "__init__.py", GEN_DIR / "android" / "os" / "__init__.py"]:
        if not p.exists():
            p.write_text("", encoding="utf-8")

    args = [
        "protoc",
        f"-I{PROTO_ROOT}",
        f"--python_out={GEN_DIR}",
        str(PROTO_PATH),
    ]
    rc = protoc.main(args)
    if rc != 0 or not target.exists():
        raise RuntimeError(f"protoc failed with exit code {rc}")


@dataclass(frozen=True)
class BsMinSnapshot:
    battery_realtime_ms: int | None
    battery_uptime_ms: int | None
    screen_off_realtime_ms: int | None
    screen_off_uptime_ms: int | None
    screen_doze_duration_ms: int | None

    total_mah: int | None
    total_mah_screen_off: int | None
    total_mah_screen_doze: int | None
    total_mah_light_doze: int | None
    total_mah_deep_doze: int | None

    screen_on_duration_ms: int | None
    interactive_duration_ms: int | None


def _get_int(obj, attr: str) -> int | None:
    if obj is None or not hasattr(obj, attr):
        return None
    v = getattr(obj, attr)
    try:
        if hasattr(obj, "HasField") and obj.HasField(attr) is False:
            return None
    except Exception:
        pass
    try:
        return int(v)
    except Exception:
        return None


def load_batterystats_min_snapshot(pb_path: Path) -> BsMinSnapshot:
    _ensure_generated()
    sys.path.insert(0, str(GEN_DIR))
    from android.os import batterystats_min_pb2  # type: ignore

    dump = batterystats_min_pb2.BatteryStatsServiceDumpProto()
    dump.ParseFromString(pb_path.read_bytes())

    msg = dump.batterystats if dump.HasField("batterystats") else None
    system = msg.system if msg and msg.HasField("system") else None
    battery = system.battery if system and system.HasField("battery") else None
    discharge = system.battery_discharge if system and system.HasField("battery_discharge") else None
    misc = system.misc if system and system.HasField("misc") else None

    return BsMinSnapshot(
        battery_realtime_ms=_get_int(battery, "battery_realtime_ms"),
        battery_uptime_ms=_get_int(battery, "battery_uptime_ms"),
        screen_off_realtime_ms=_get_int(battery, "screen_off_realtime_ms"),
        screen_off_uptime_ms=_get_int(battery, "screen_off_uptime_ms"),
        screen_doze_duration_ms=_get_int(battery, "screen_doze_duration_ms"),
        total_mah=_get_int(discharge, "total_mah"),
        total_mah_screen_off=_get_int(discharge, "total_mah_screen_off"),
        total_mah_screen_doze=_get_int(discharge, "total_mah_screen_doze"),
        total_mah_light_doze=_get_int(discharge, "total_mah_light_doze"),
        total_mah_deep_doze=_get_int(discharge, "total_mah_deep_doze"),
        screen_on_duration_ms=_get_int(misc, "screen_on_duration_ms"),
        interactive_duration_ms=_get_int(misc, "interactive_duration_ms"),
    )


def diff_batterystats_min(a: BsMinSnapshot, b: BsMinSnapshot) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    a_dict = asdict(a)
    b_dict = asdict(b)
    for k, va in a_dict.items():
        vb = b_dict[k]
        if vb is None:
            out[k] = None
        elif va is None:
            out[k] = int(vb)
        else:
            out[k] = int(vb) - int(va)
    return out


def derive_batterystats_min(delta: dict[str, int | None] | None) -> dict[str, float | None]:
    if not delta:
        return {}

    def f(key: str) -> float | None:
        v = delta.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    dt_ms = f("battery_realtime_ms")
    d_screen_on_ms = f("screen_on_duration_ms")
    d_total_mah = f("total_mah")
    d_total_mah_screen_off = f("total_mah_screen_off")

    out: dict[str, float | None] = {}
    if dt_ms and dt_ms > 0:
        out["duration_s"] = dt_ms / 1000.0
        if d_total_mah is not None:
            out["avg_current_mA"] = d_total_mah / (dt_ms / 3600000.0)
        if d_total_mah_screen_off is not None:
            out["avg_current_screen_off_mA"] = d_total_mah_screen_off / (dt_ms / 3600000.0)
    if d_screen_on_ms and d_screen_on_ms > 0:
        out["screen_on_duration_s"] = d_screen_on_ms / 1000.0
        if d_total_mah is not None:
            out["avg_current_per_screen_on_hour_mA"] = d_total_mah / (d_screen_on_ms / 3600000.0)
    return out


def write_batterystats_min_summary(
    *,
    start_pb: Path | None,
    end_pb: Path,
    out_json: Path,
    out_csv: Path,
    label: str | None = None,
) -> None:
    end = load_batterystats_min_snapshot(end_pb)
    start = load_batterystats_min_snapshot(start_pb) if start_pb else None
    delta = diff_batterystats_min(start, end) if start else None
    derived = derive_batterystats_min(delta)

    payload = {
        "label": label,
        "start_pb": str(start_pb) if start_pb else None,
        "end_pb": str(end_pb),
        "end": asdict(end),
        "delta": delta,
        "derived": derived,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    keys = list(asdict(end).keys())
    lines = ["metric,value"]
    for k in keys:
        v = getattr(end, k)
        lines.append(f"{k},{'' if v is None else v}")
    if delta is not None:
        for k in keys:
            v = delta.get(k)
            lines.append(f"delta_{k},{'' if v is None else v}")
        for k in sorted(derived.keys()):
            v = derived[k]
            lines.append(f"derived_{k},{'' if v is None else v}")

    out_csv.write_text("\n".join(lines) + "\n", encoding="utf-8")


# -----------------------------
# Enrich + report
# -----------------------------


@dataclass
class ClusterTable:
    freqs_khz: list[int]
    current_ma: list[float]


def _load_cluster_csv(path: Path) -> ClusterTable:
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


def _load_mapping(path: Path) -> dict[int, int]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    mp = obj.get("mapping_policy_to_cluster") or {}
    out: dict[int, int] = {}
    for k, v in mp.items():
        out[int(k)] = int(v)
    return out


def _load_power_profile_items(profile_json: Path) -> dict[str, float]:
    try:
        obj = json.loads(profile_json.read_text(encoding="utf-8"))
    except Exception:
        return {}

    items = obj.get("items_ma") or {}
    out: dict[str, float] = {}
    if isinstance(items, dict):
        for k, v in items.items():
            try:
                out[str(k)] = float(v)
            except Exception:
                continue
    return out


def _parse_ts(ts: str) -> datetime | None:
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def enrich_run_with_cpu_energy(
    *,
    run_csv: Path,
    out_csv: Path,
    map_json: Path = Path("artifacts/android/power_profile/policy_cluster_map.json"),
    clusters_dir: Path = Path("artifacts/android/power_profile"),
    profile_json: Path = Path("artifacts/android/power_profile/power_profile.json"),
    voltage_col: str = "battery_voltage_mv",
    charge_col: str = "charge_counter_uAh",
    brightness_col: str = "brightness",
    brightness_max: float = 255.0,
) -> None:
    mapping = _load_mapping(map_json)

    items_ma = _load_power_profile_items(profile_json)
    screen_on_ma = items_ma.get("screen.on")
    screen_full_ma = items_ma.get("screen.full")

    cluster_tables: dict[int, ClusterTable] = {}
    for cluster in sorted(set(mapping.values())):
        cluster_tables[cluster] = _load_cluster_csv(clusters_dir / f"cluster{cluster}_freq_power.csv")

    policy_lookup: dict[int, dict[int, float]] = {}
    policy_freqs_sorted: dict[int, list[int]] = {}
    for policy, cluster in mapping.items():
        table = cluster_tables[cluster]
        policy_lookup[policy] = {f: ma for f, ma in zip(table.freqs_khz, table.current_ma)}
        policy_freqs_sorted[policy] = sorted(table.freqs_khz)

    with run_csv.open("r", encoding="utf-8", newline="") as fin:
        reader = csv.DictReader(fin)
        in_fields = list(reader.fieldnames or [])

        out_fields = list(in_fields)
        for policy in sorted(mapping.keys()):
            for suffix in ["energy_mJ", "energy_mJ_matched", "energy_mJ_unmatched", "avg_power_mW"]:
                col = f"cpu_policy{policy}_{suffix}"
                if col not in out_fields:
                    out_fields.append(col)
            col = f"cpu_policy{policy}_unmatched_dt_ms"
            if col not in out_fields:
                out_fields.append(col)
        for col in ["cpu_energy_mJ_total", "battery_discharge_energy_mJ", "dt_s"]:
            if col not in out_fields:
                out_fields.append(col)

        for col in ["screen_brightness_norm", "screen_power_mW_est", "screen_energy_mJ_est"]:
            if col not in out_fields:
                out_fields.append(col)

        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", encoding="utf-8", newline="") as fout:
            writer = csv.DictWriter(fout, fieldnames=out_fields)
            writer.writeheader()

            prev_ts: datetime | None = None
            prev_charge_uah: int | None = None

            for row in reader:
                ts = _parse_ts(row.get("ts_pc", ""))
                dt_s = ""
                if ts and prev_ts:
                    dt = (ts - prev_ts).total_seconds()
                    if dt >= 0:
                        dt_s = f"{dt:.3f}"
                prev_ts = ts or prev_ts

                voltage_mv = None
                v_raw = row.get(voltage_col, "")
                if v_raw not in (None, ""):
                    try:
                        voltage_mv = int(float(v_raw))
                    except Exception:
                        voltage_mv = None

                brightness_norm = None
                b_raw = row.get(brightness_col, "")
                if b_raw not in (None, ""):
                    try:
                        b_val = float(b_raw)
                        bmax = float(brightness_max) if float(brightness_max) > 0 else 255.0
                        brightness_norm = max(0.0, min(1.0, b_val / bmax))
                    except Exception:
                        brightness_norm = None

                cpu_total = 0.0

                for policy in sorted(mapping.keys()):
                    lookup = policy_lookup.get(policy, {})
                    freqs_sorted = policy_freqs_sorted.get(policy, [])

                    matched_mw_ms = 0.0
                    unmatched_mw_ms = 0.0
                    sum_dt_ms = 0
                    unmatched_dt_ms = 0

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
                            if not freqs_sorted:
                                unmatched_dt_ms += dt_ms
                                continue
                            nearest = min(freqs_sorted, key=lambda f: abs(f - freq))
                            cur_ma = lookup.get(nearest)
                            if cur_ma is None:
                                unmatched_dt_ms += dt_ms
                                continue
                            if voltage_mv is None:
                                unmatched_dt_ms += dt_ms
                                continue
                            power_mw = float(cur_ma) * float(voltage_mv) / 1000.0
                            unmatched_mw_ms += power_mw * float(dt_ms)
                            continue

                        if voltage_mv is None:
                            unmatched_dt_ms += dt_ms
                            continue
                        power_mw = float(cur_ma) * float(voltage_mv) / 1000.0
                        matched_mw_ms += power_mw * float(dt_ms)

                    energy_mJ = (matched_mw_ms + unmatched_mw_ms) / 1000.0
                    energy_mJ_matched = matched_mw_ms / 1000.0
                    energy_mJ_unmatched = unmatched_mw_ms / 1000.0
                    cpu_total += energy_mJ

                    row[f"cpu_policy{policy}_energy_mJ"] = f"{energy_mJ:.3f}" if voltage_mv is not None else ""
                    row[f"cpu_policy{policy}_energy_mJ_matched"] = (
                        f"{energy_mJ_matched:.3f}" if voltage_mv is not None else ""
                    )
                    row[f"cpu_policy{policy}_energy_mJ_unmatched"] = (
                        f"{energy_mJ_unmatched:.3f}" if voltage_mv is not None else ""
                    )
                    row[f"cpu_policy{policy}_unmatched_dt_ms"] = str(unmatched_dt_ms)

                    if sum_dt_ms > 0:
                        avg_p = (energy_mJ * 1000.0) / float(sum_dt_ms)
                        row[f"cpu_policy{policy}_avg_power_mW"] = f"{avg_p:.3f}" if voltage_mv is not None else ""
                    else:
                        row[f"cpu_policy{policy}_avg_power_mW"] = ""

                row["cpu_energy_mJ_total"] = f"{cpu_total:.3f}" if voltage_mv is not None else ""
                row["dt_s"] = dt_s

                row["screen_brightness_norm"] = f"{brightness_norm:.6f}" if brightness_norm is not None else ""
                screen_power_mw = None
                if (
                    voltage_mv is not None
                    and brightness_norm is not None
                    and screen_on_ma is not None
                    and screen_full_ma is not None
                ):
                    try:
                        screen_current_ma = float(screen_on_ma) + float(screen_full_ma) * float(brightness_norm)
                        screen_power_mw = screen_current_ma * float(voltage_mv) / 1000.0
                        row["screen_power_mW_est"] = f"{screen_power_mw:.3f}"
                    except Exception:
                        row["screen_power_mW_est"] = ""
                else:
                    row["screen_power_mW_est"] = ""

                if screen_power_mw is not None and dt_s not in (None, ""):
                    try:
                        dts = float(dt_s)
                        if dts > 0:
                            row["screen_energy_mJ_est"] = f"{(float(screen_power_mw) * dts):.3f}"
                        else:
                            row["screen_energy_mJ_est"] = ""
                    except Exception:
                        row["screen_energy_mJ_est"] = ""
                else:
                    row["screen_energy_mJ_est"] = ""

                discharge_mJ = ""
                ch_raw = row.get(charge_col, "")
                if ch_raw not in (None, ""):
                    try:
                        charge_uah = int(float(ch_raw))
                    except Exception:
                        charge_uah = None
                    if charge_uah is not None and prev_charge_uah is not None and voltage_mv is not None:
                        d_uah = charge_uah - prev_charge_uah
                        discharge_mJ_val = (-float(d_uah)) * float(voltage_mv) * 0.0036
                        discharge_mJ = f"{discharge_mJ_val:.3f}"
                    prev_charge_uah = charge_uah if charge_uah is not None else prev_charge_uah

                row["battery_discharge_energy_mJ"] = discharge_mJ

                writer.writerow({k: row.get(k, "") for k in out_fields})


@dataclass
class RunSummary:
    start_ts: str
    end_ts: str
    duration_s: float
    rows: int
    mean_voltage_mv: float | None
    mean_batt_power_mw: float | None
    mean_batt_power_mw_perfetto: float | None
    batt_power_source_preferred: str
    mean_cpu_power_mw: float | None


def report_run(csv_path: Path, out_dir: Path | None = None) -> tuple[Path, Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if out_dir is None:
        out_dir = Path("artifacts") / "reports" / csv_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    def parse_ts(s: str) -> datetime | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    df = pd.read_csv(csv_path)

    out = df.copy()
    if "ts_pc" in out.columns:
        ts = out["ts_pc"].astype(str).map(parse_ts)
        out["t_s"] = (ts - ts.iloc[0]).dt.total_seconds() if hasattr(ts, "dt") else None

    for col in ["battery_voltage_mv", "charge_counter_uAh", "cpu_energy_mJ_total", "brightness"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    if "battery_voltage_mv" in out.columns and "charge_counter_uAh" in out.columns and "t_s" in out.columns:
        d_uah = out["charge_counter_uAh"].diff()
        dt_s = out["t_s"].diff()
        out["batt_discharge_current_mA"] = (-d_uah * 3.6) / dt_s
        out["batt_discharge_power_mW"] = out["batt_discharge_current_mA"] * out["battery_voltage_mv"] / 1000.0

    if "cpu_energy_mJ_total" in out.columns:
        if "t_s" in out.columns:
            dt_s = out["t_s"].diff()
            out["cpu_power_mW_total"] = out["cpu_energy_mJ_total"] / dt_s
        elif "dt_s" in out.columns:
            out["dt_s"] = pd.to_numeric(out["dt_s"], errors="coerce")
            out["cpu_power_mW_total"] = out["cpu_energy_mJ_total"] / out["dt_s"]

    rows = int(len(out))
    start_ts = str(out["ts_pc"].iloc[0]) if "ts_pc" in out.columns and rows else ""
    end_ts = str(out["ts_pc"].iloc[-1]) if "ts_pc" in out.columns and rows else ""
    duration_s = float(out["t_s"].iloc[-1]) if "t_s" in out.columns and rows else float("nan")

    mean_voltage_mv = float(out["battery_voltage_mv"].mean()) if "battery_voltage_mv" in out.columns else None
    mean_batt_power_mw = (
        float(out["batt_discharge_power_mW"].mean()) if "batt_discharge_power_mW" in out.columns else None
    )
    mean_cpu_power_mw = float(out["cpu_power_mW_total"].mean()) if "cpu_power_mW_total" in out.columns else None

    # Optional: prefer Perfetto android.power battery counters if present in the report dir.
    pf_ts_path = out_dir / "perfetto_android_power_timeseries.csv"
    pf_summary_path = out_dir / "perfetto_android_power_summary.csv"
    mean_batt_power_mw_perfetto: float | None = None
    pf_power_ts: pd.Series | None = None
    pf_x: pd.Series | None = None
    if pf_ts_path.exists():
        try:
            pf_ts = pd.read_csv(pf_ts_path)
            if "t_s" in pf_ts.columns and "power_mw_calc" in pf_ts.columns:
                pf_x = pd.to_numeric(pf_ts["t_s"], errors="coerce")
                pf_power_ts = pd.to_numeric(pf_ts["power_mw_calc"], errors="coerce")
                if pf_power_ts.notna().any():
                    mean_batt_power_mw_perfetto = float(pf_power_ts.mean())
        except Exception:
            # Keep report generation best-effort.
            pf_x = None
            pf_power_ts = None

    batt_power_source_preferred = "charge_counter_diff"
    if pf_summary_path.exists() or mean_batt_power_mw_perfetto is not None:
        batt_power_source_preferred = "perfetto_android_power"

    summary = RunSummary(
        start_ts=start_ts,
        end_ts=end_ts,
        duration_s=duration_s,
        rows=rows,
        mean_voltage_mv=mean_voltage_mv,
        mean_batt_power_mw=mean_batt_power_mw,
        mean_batt_power_mw_perfetto=mean_batt_power_mw_perfetto,
        batt_power_source_preferred=batt_power_source_preferred,
        mean_cpu_power_mw=mean_cpu_power_mw,
    )

    md_path = out_dir / "summary.md"
    png_path = out_dir / "timeseries.png"

    lines: list[str] = []
    lines.append("# Run report")
    lines.append("")
    lines.append(f"- source: {csv_path.as_posix()}")
    lines.append(f"- rows: {summary.rows}")
    if summary.start_ts:
        lines.append(f"- start: {summary.start_ts}")
    if summary.end_ts:
        lines.append(f"- end: {summary.end_ts}")
    if summary.duration_s == summary.duration_s:
        lines.append(f"- duration_s: {summary.duration_s:.1f}")
    if summary.mean_voltage_mv is not None:
        lines.append(f"- mean_voltage_mv: {summary.mean_voltage_mv:.1f}")
    lines.append(f"- battery_power_source_preferred: {summary.batt_power_source_preferred}")
    if summary.mean_batt_power_mw is not None:
        lines.append(f"- mean_batt_discharge_power_mW_charge_counter: {summary.mean_batt_power_mw:.1f}")
    if summary.mean_batt_power_mw_perfetto is not None:
        lines.append(f"- mean_batt_power_mW_perfetto: {summary.mean_batt_power_mw_perfetto:.1f}")
    if summary.mean_cpu_power_mw is not None:
        lines.append(f"- mean_cpu_power_mW_total: {summary.mean_cpu_power_mw:.1f}")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fig, axes = plt.subplots(4, 1, figsize=(11, 14), sharex=True)
    x = out["t_s"] if "t_s" in out.columns else range(len(out))

    if "battery_voltage_mv" in out.columns:
        axes[0].plot(x, out["battery_voltage_mv"], label="battery_voltage_mv")
        axes[0].set_ylabel("mV")
        axes[0].legend(loc="best")

    thermal_cols = [c for c in out.columns if c.startswith("thermal_") and c.endswith("_C")]
    if thermal_cols:
        for c in thermal_cols:
            axes[1].plot(x, out[c], label=c)
        axes[1].set_ylabel("C")
        axes[1].legend(loc="best", ncols=2)

    if "cpu_power_mW_total" in out.columns:
        axes[2].plot(x, out["cpu_power_mW_total"], label="cpu_power_mW_total")
        axes[2].set_ylabel("mW")
        axes[2].legend(loc="best")

    if "brightness" in out.columns:
        axes[3].plot(x, out["brightness"], label="brightness", color="tab:orange")
    if "batt_discharge_power_mW" in out.columns:
        axes[3].plot(x, out["batt_discharge_power_mW"], label="batt_discharge_power_mW", color="tab:green")
    if pf_x is not None and pf_power_ts is not None:
        axes[3].plot(pf_x, pf_power_ts, label="perfetto_power_mw_calc", color="tab:blue", alpha=0.9)
    axes[3].set_ylabel("brightness / mW")
    axes[3].legend(loc="best")

    axes[-1].set_xlabel("t (s)")
    fig.tight_layout()
    fig.savefig(png_path, dpi=160)
    plt.close(fig)

    return md_path, png_path


# -----------------------------
# Module CLI
# -----------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="mp_power internal ops (used by pipeline; exposed for debugging)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pp = sub.add_parser("parse-power-profile", help="Parse power_profile xmltree into JSON/CSVs")
    p_pp.add_argument("xmltree", type=Path)
    p_pp.add_argument("--out-dir", type=Path, default=Path("artifacts/android/power_profile"))

    p_perf = sub.add_parser("parse-perfetto-android-power", help="Parse Perfetto android.power battery counters")
    p_perf.add_argument("--trace", type=Path, required=True)
    p_perf.add_argument("--out-dir", type=Path, default=None)
    p_perf.add_argument("--label", default="")
    p_perf.add_argument("--no-timeseries", action="store_true")

    p_mark = sub.add_parser("parse-perfetto-policy-markers", help="Parse Perfetto slices for policy markers")
    p_mark.add_argument("--trace", type=Path, required=True)
    p_mark.add_argument("--out-dir", type=Path, default=None)
    p_mark.add_argument(
        "--keywords",
        default="power,PowerHAL,powerhal,boost,hint,mtk,mi,fpsgo,uclamp,cpuset,thermal,throttle",
    )

    p_bs = sub.add_parser("parse-batterystats-proto-min", help="Parse minimal batterystats proto schema")
    p_bs.add_argument("--start", type=Path, default=None)
    p_bs.add_argument("--end", type=Path, required=True)
    p_bs.add_argument("--out-json", type=Path, required=True)
    p_bs.add_argument("--out-csv", type=Path, required=True)
    p_bs.add_argument("--label", default=None)

    p_en = sub.add_parser("enrich", help="Enrich a run CSV with CPU energy + screen estimate")
    p_en.add_argument("--run-csv", type=Path, required=True)
    p_en.add_argument("--out", type=Path, required=True)

    p_rep = sub.add_parser("report", help="Generate report (summary.md + timeseries.png)")
    p_rep.add_argument("--csv", type=Path, required=True)
    p_rep.add_argument("--out-dir", type=Path, default=None)

    args = ap.parse_args(argv)

    if args.cmd == "parse-power-profile":
        profile = parse_power_profile_xmltree(args.xmltree)
        write_power_profile_outputs(profile, args.out_dir)
        return 0

    if args.cmd == "parse-perfetto-android-power":
        parse_perfetto_android_power_counters(args.trace, out_dir=args.out_dir, label=args.label, no_timeseries=args.no_timeseries)
        return 0

    if args.cmd == "parse-perfetto-policy-markers":
        kws = [k.strip() for k in str(args.keywords).split(",") if k.strip()]
        parse_perfetto_policy_markers(args.trace, out_dir=args.out_dir, keywords=kws)
        return 0

    if args.cmd == "parse-batterystats-proto-min":
        os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
        write_batterystats_min_summary(
            start_pb=args.start,
            end_pb=args.end,
            out_json=args.out_json,
            out_csv=args.out_csv,
            label=args.label,
        )
        return 0

    if args.cmd == "enrich":
        enrich_run_with_cpu_energy(run_csv=args.run_csv, out_csv=args.out)
        return 0

    if args.cmd == "report":
        report_run(args.csv, out_dir=args.out_dir)
        return 0

    raise SystemExit("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
