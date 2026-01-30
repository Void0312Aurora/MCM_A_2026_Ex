from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PowerProfile:
    clusters_cores: list[int]
    core_speeds_khz: dict[int, list[int]]
    core_power_ma: dict[int, list[float]]
    battery_capacity_mah: int | None


_RE_ARRAY_HEADER = re.compile(r"^\s*E: array .*\n\s*A: name=\"(?P<name>[^\"]+)\"", re.MULTILINE)
_RE_VALUE = re.compile(r"\n\s*E: value .*?\n\s*T: '(?P<val>[^']*)'", re.MULTILINE)


def _extract_array(text: str, key: str) -> list[str] | None:
    # Find the first occurrence of array with matching name, then capture subsequent E:value T:'..'
    idx = text.find(f'A: name="{key}"')
    if idx < 0:
        return None

    # Restrict to a window until the next "E: array" at same indent, or end.
    # The aapt2 xmltree output is small; a simple heuristic window is sufficient.
    window = text[idx:]
    next_idx = window.find('\n    E: array', 1)
    if next_idx > 0:
        window = window[:next_idx]

    return [m.group('val') for m in _RE_VALUE.finditer(window)]


def parse_profile(xmltree_path: Path) -> PowerProfile:
    text = xmltree_path.read_text(encoding="utf-8", errors="replace")

    clusters_cores = []
    cores_vals = _extract_array(text, 'cpu.clusters.cores')
    if cores_vals:
        clusters_cores = [int(float(v)) for v in cores_vals]

    core_speeds_khz: dict[int, list[int]] = {}
    core_power_ma: dict[int, list[float]] = {}

    for cluster in range(0, 8):
        speeds = _extract_array(text, f'cpu.core_speeds.cluster{cluster}')
        power = _extract_array(text, f'cpu.core_power.cluster{cluster}')
        if speeds:
            core_speeds_khz[cluster] = [int(float(v)) for v in speeds]
        if power:
            core_power_ma[cluster] = [float(v) for v in power]

    cap = None
    cap_vals = None
    # capacity is an item, not array; parse via regex
    m = re.search(r"A: name=\"battery.capacity\".*?\n\s*T: '(?P<cap>[^']+)'", text, re.DOTALL)
    if m:
        try:
            cap = int(float(m.group('cap')))
        except Exception:
            cap = None
    _ = cap_vals

    return PowerProfile(
        clusters_cores=clusters_cores,
        core_speeds_khz=core_speeds_khz,
        core_power_ma=core_power_ma,
        battery_capacity_mah=cap,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse aapt2 xmltree power_profile overlay into JSON/CSV")
    parser.add_argument(
        "xmltree",
        type=Path,
        help="Path to aapt2 dump xmltree output (e.g., FrameworkResOverlay_power_profile_xmltree.txt)",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("artifacts/android/power_profile"))
    args = parser.parse_args()

    profile = parse_profile(args.xmltree)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "power_profile.json").write_text(
        json.dumps(
            {
                "battery_capacity_mah": profile.battery_capacity_mah,
                "clusters_cores": profile.clusters_cores,
                "core_speeds_khz": {str(k): v for k, v in profile.core_speeds_khz.items()},
                "core_power_ma": {str(k): v for k, v in profile.core_power_ma.items()},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # CSV per cluster
    for cluster, speeds in profile.core_speeds_khz.items():
        powers = profile.core_power_ma.get(cluster, [])
        rows = ["freq_khz,power_ma"]
        for i, f in enumerate(speeds):
            p = powers[i] if i < len(powers) else ""
            rows.append(f"{f},{p}")
        (out_dir / f"cluster{cluster}_freq_power.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
