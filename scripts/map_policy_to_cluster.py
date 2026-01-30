from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PolicyInfo:
    policy: int
    related_cpus: list[int]
    freqs_khz: list[int]


def _run(adb: str, args: list[str], timeout_s: float) -> tuple[int, str, str]:
    proc = subprocess.run(
        [adb, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def _shell_cat(adb: str, serial: str | None, path: str, timeout_s: float) -> str | None:
    base = ["-s", serial] if serial else []
    rc, out, err = _run(adb, [*base, "shell", "cat", path], timeout_s=timeout_s)
    if rc != 0:
        if "No such file" in (out + err):
            return None
        return None
    return out


def _parse_int_list(text: str) -> list[int]:
    out: list[int] = []
    for tok in text.replace("\n", " ").split():
        try:
            out.append(int(tok))
        except Exception:
            continue
    return out


def _parse_time_in_state_freqs(text: str) -> list[int]:
    freqs: list[int] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            freqs.append(int(parts[0]))
        except Exception:
            continue
    return freqs


def read_policy_info(adb: str, serial: str | None, policy: int) -> PolicyInfo | None:
    rel = _shell_cat(adb, serial, f"/sys/devices/system/cpu/cpufreq/policy{policy}/related_cpus", timeout_s=5.0)
    tis = _shell_cat(adb, serial, f"/sys/devices/system/cpu/cpufreq/policy{policy}/stats/time_in_state", timeout_s=5.0)
    if rel is None or tis is None:
        return None
    related = _parse_int_list(rel)
    freqs = _parse_time_in_state_freqs(tis)
    return PolicyInfo(policy=policy, related_cpus=related, freqs_khz=freqs)


def load_power_profile(profile_json: Path) -> dict:
    return json.loads(profile_json.read_text(encoding="utf-8"))


def _cluster_freqs(profile: dict, cluster: int) -> list[int]:
    core_speeds = profile.get("core_speeds_khz", {})
    vals = core_speeds.get(str(cluster))
    if not vals:
        return []
    return [int(v) for v in vals]


def _cluster_cores(profile: dict, cluster: int) -> int | None:
    cores = profile.get("clusters_cores")
    if not cores:
        return None
    if 0 <= cluster < len(cores):
        try:
            return int(cores[cluster])
        except Exception:
            return None
    return None


def score_policy_cluster(policy: PolicyInfo, profile: dict, cluster: int) -> float:
    c_freqs = _cluster_freqs(profile, cluster)
    if not c_freqs:
        return float("-inf")

    p_freqs = set(policy.freqs_khz)
    c_set = set(c_freqs)

    inter = len(p_freqs & c_set)
    union = len(p_freqs | c_set)
    jaccard = inter / union if union else 0.0

    score = 100.0 * jaccard

    c_cores = _cluster_cores(profile, cluster)
    if c_cores is not None:
        if c_cores == len(policy.related_cpus):
            score += 25.0
        else:
            score -= 5.0 * abs(c_cores - len(policy.related_cpus))

    # Bonus for same max freq (common strong signal)
    try:
        if max(c_freqs) == max(policy.freqs_khz):
            score += 15.0
    except Exception:
        pass

    return score


def main() -> int:
    parser = argparse.ArgumentParser(description="Infer cpufreq policy->power_profile cluster mapping")
    parser.add_argument("--adb", default="adb", help="Path to adb")
    parser.add_argument("--serial", default=None, help="Device serial (optional)")
    parser.add_argument(
        "--profile-json",
        type=Path,
        default=Path("artifacts/android/power_profile/power_profile.json"),
        help="Parsed power_profile.json",
    )
    parser.add_argument(
        "--policies",
        default=None,
        help="Comma-separated policies to map (default: auto 0..15 that exist)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/android/power_profile/policy_cluster_map.json"),
        help="Output mapping JSON",
    )
    args = parser.parse_args()

    profile = load_power_profile(args.profile_json)

    clusters = sorted(int(k) for k in profile.get("core_speeds_khz", {}).keys())
    if not clusters:
        raise SystemExit(f"No clusters found in {args.profile_json}")

    if args.policies:
        policies = [int(x.strip()) for x in args.policies.split(",") if x.strip()]
    else:
        policies = list(range(0, 16))

    policy_infos: list[PolicyInfo] = []
    for p in policies:
        info = read_policy_info(args.adb, args.serial, p)
        if info is not None:
            policy_infos.append(info)

    if not policy_infos:
        raise SystemExit("No cpufreq policies found/readable. Is the device connected and permissions OK?")

    mapping: dict[str, int] = {}
    debug: dict[str, dict[str, float]] = {}

    for pi in policy_infos:
        scores: dict[int, float] = {}
        for c in clusters:
            scores[c] = score_policy_cluster(pi, profile, c)
        best_cluster = max(scores, key=lambda k: scores[k])
        mapping[str(pi.policy)] = int(best_cluster)
        debug[str(pi.policy)] = {str(k): float(v) for k, v in scores.items()}

    out_obj = {
        "serial": args.serial,
        "mapped_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "mapping_policy_to_cluster": mapping,
        "debug_scores": debug,
        "notes": "Mapping inferred by freq overlap + core count; verify once per device/ROM.",
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Policy->cluster mapping:")
    for p in sorted(mapping, key=lambda x: int(x)):
        print(f"  policy{p} -> cluster{mapping[p]}")
    print(f"Wrote: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
