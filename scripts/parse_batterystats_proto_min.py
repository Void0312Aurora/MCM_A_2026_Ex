"""Parse `dumpsys batterystats --proto` using a minimal proto schema.

This is used as a *short-window* energy metric when instantaneous current is unavailable.

Typical workflow for a 9-minute run:
  1) adb shell dumpsys batterystats --reset
  2) run experiment
  3) adb exec-out "dumpsys batterystats --proto" > batterystats_end.pb

This tool can also take both start/end protos and compute a delta.

Outputs a JSON summary and a small CSV for quick plotting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


PROTO_PATH = Path(__file__).parent / "proto" / "android" / "os" / "batterystats_min.proto"
GEN_DIR = Path(__file__).parent / "_generated"
PROTO_ROOT = Path(__file__).parent / "proto"


def _ensure_generated() -> None:
    """Generate python bindings for batterystats_min.proto if missing."""
    target = GEN_DIR / "android" / "os" / "batterystats_min_pb2.py"
    if target.exists() and target.stat().st_mtime >= PROTO_PATH.stat().st_mtime:
        return

    try:
        from grpc_tools import protoc
    except Exception as e:
        raise RuntimeError("grpcio-tools is required to generate proto bindings") from e

    # Ensure package dirs are importable.
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
    # Timing
    battery_realtime_ms: int | None
    battery_uptime_ms: int | None
    screen_off_realtime_ms: int | None
    screen_off_uptime_ms: int | None
    screen_doze_duration_ms: int | None

    # Discharge buckets (mAh)
    total_mah: int | None
    total_mah_screen_off: int | None
    total_mah_screen_doze: int | None
    total_mah_light_doze: int | None
    total_mah_deep_doze: int | None

    # Misc (ms)
    screen_on_duration_ms: int | None
    interactive_duration_ms: int | None


def _get_int(obj, attr: str) -> int | None:
    if obj is None:
        return None
    if not hasattr(obj, attr):
        return None
    v = getattr(obj, attr)
    # proto2 scalars default to 0 when not set, but has_field tells us if set.
    try:
        if hasattr(obj, f"HasField") and obj.HasField(attr) is False:
            return None
    except Exception:
        pass
    try:
        return int(v)
    except Exception:
        return None


def load_snapshot(pb_path: Path) -> BsMinSnapshot:
    _ensure_generated()

    # Import after generation.
    sys.path.insert(0, str(GEN_DIR))
    try:
        from android.os import batterystats_min_pb2  # type: ignore
    finally:
        # Leave sys.path entry in place; repeated calls in same process are fine.
        pass

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


def diff(a: BsMinSnapshot, b: BsMinSnapshot) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for k, va in asdict(a).items():
        vb = asdict(b)[k]
        # Some fields are absent (unset) in the immediate snapshot after `batterystats --reset`.
        # For short-window experiments we treat missing START values as 0 when END is present.
        if vb is None:
            out[k] = None
        elif va is None:
            out[k] = int(vb)
        else:
            out[k] = int(vb) - int(va)
    return out


def derive(delta: dict[str, int | None] | None) -> dict[str, float | None]:
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


def _write_csv(path: Path, snapshot: BsMinSnapshot, delta: dict[str, int | None] | None) -> None:
    keys = list(asdict(snapshot).keys())
    lines = ["metric,value"]
    for k in keys:
        v = getattr(snapshot, k)
        lines.append(f"{k},{'' if v is None else v}")
    if delta is not None:
        for k in keys:
            v = delta.get(k)
            lines.append(f"delta_{k},{'' if v is None else v}")

        derived = derive(delta)
        for k in sorted(derived.keys()):
            v = derived[k]
            lines.append(f"derived_{k},{'' if v is None else v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=Path, default=None, help="Optional start proto (for delta)")
    ap.add_argument("--end", type=Path, required=True, help="End proto")
    ap.add_argument("--out-json", type=Path, required=True)
    ap.add_argument("--out-csv", type=Path, required=True)
    ap.add_argument("--label", type=str, default=None)
    args = ap.parse_args()

    end = load_snapshot(args.end)
    start = load_snapshot(args.start) if args.start else None
    delta = diff(start, end) if start else None
    derived = derive(delta)

    payload = {
        "label": args.label,
        "start_pb": str(args.start) if args.start else None,
        "end_pb": str(args.end),
        "end": asdict(end),
        "delta": delta,
        "derived": derived,
    }

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_csv(args.out_csv, end, delta)

    print(f"Wrote: {args.out_json}")
    print(f"Wrote: {args.out_csv}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
    raise SystemExit(main())
