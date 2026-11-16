"""Observe one benchmark process without changing its inference settings."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil


def memory_snapshot() -> dict:
    ram, swap = psutil.virtual_memory(), psutil.swap_memory()
    return {
        "ram_total_bytes": ram.total,
        "ram_available_bytes": ram.available,
        "ram_percent": ram.percent,
        "swap_total_bytes": swap.total,
        "swap_used_bytes": swap.used,
    }


def terminate_owned(process: subprocess.Popen, tree: list) -> None:
    """Only terminate the process we spawned and its previously found children."""
    for member in reversed(tree):
        try:
            if member.is_running():
                member.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("missing command after --")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.resources.parent.mkdir(parents=True, exist_ok=True)
    before = memory_snapshot()
    observations, errors = [], []
    started = time.perf_counter()
    with args.log.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        root = psutil.Process(process.pid)
        tree = [root]
        # Windows venv's python.exe is a tiny launcher. Discover its interpreter
        # early, before model loading. Never repeat an expensive whole-system
        # process snapshot during inference or let a sampling failure orphan it.
        for _ in range(3):
            time.sleep(0.2)
            if process.poll() is not None:
                break
            try:
                descendants = root.children(recursive=True)
                if descendants:
                    tree = [root, *descendants]
                    break
            except (psutil.Error, OSError) as error:
                errors.append(f"early_process_discovery: {error}")
        pid_record = {
            "root_pid": process.pid,
            "early_process_tree_pids": [member.pid for member in tree],
            "command": command,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        args.resources.with_suffix(".pid.json").write_text(
            json.dumps(pid_record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            while process.poll() is None:
                observation = {"elapsed_seconds": time.perf_counter() - started}
                try:
                    observation.update(memory_snapshot())
                except (psutil.Error, OSError) as error:
                    errors.append(f"host_memory_sample: {error}")
                stats = []
                for member in tree:
                    try:
                        stats.append(member.memory_info())
                    except (psutil.Error, OSError) as error:
                        errors.append(f"process_memory_sample: {error}")
                observation["process_tree_rss_bytes"] = sum(info.rss for info in stats)
                observation["process_tree_vms_bytes"] = sum(info.vms for info in stats)
                observation["process_tree_count"] = len(stats)
                observations.append(observation)
                # Avoid CUDA/NVML polling inside timed inference.
                time.sleep(2.0)
        except BaseException:
            terminate_owned(process, tree)
            raise
    after = memory_snapshot()
    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        **pid_record,
        "return_code": process.returncode,
        "wall_seconds": time.perf_counter() - started,
        "before": before,
        "after": after,
        "sample_interval_seconds": 2,
        "sampling_errors": errors,
        "observed_min_available_ram_bytes": min(
            [before["ram_available_bytes"], after["ram_available_bytes"]]
            + [row["ram_available_bytes"] for row in observations if "ram_available_bytes" in row]
        ),
        "observed_peak_process_tree_rss_bytes": max(
            [0] + [row.get("process_tree_rss_bytes", 0) for row in observations]
        ),
        "process_memory_scope": "root plus early-discovered children; no whole-system snapshots during inference",
        "observations": observations,
        "limits": [
            "RSS and host memory are sampled, not exact peaks.",
            "Shared pages can be counted more than once in process-tree RSS.",
            "Late-created children are not observed.",
            "Windows swap counters alone cannot establish active paging.",
            "The machine and existing services are not isolated or stopped.",
        ],
    }
    args.resources.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({key: result[key] for key in (
        "return_code", "wall_seconds", "observed_min_available_ram_bytes",
        "observed_peak_process_tree_rss_bytes",
    )}))
    print(f"log={args.log}; resources={args.resources}")
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
