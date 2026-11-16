"""Guarded API-only GPU switch; restores the original CPU image on failure."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent
PLAN = json.loads((OUT / "deployment_plan.json").read_text(encoding="utf-8"))
PROC_CODE = '''import json,pathlib
rows=[]
for p in pathlib.Path("/proc").iterdir():
    if p.name.isdigit():
        try:
            argv=[x.decode(errors="replace") for x in p.joinpath("cmdline").read_bytes().split(bytes([0])) if x]
            if any(x.split("/")[-1]=="uvicorn" for x in argv): rows.append({"pid":int(p.name),"argv":argv})
        except OSError: pass
print(json.dumps(rows))
'''


def inspect(name: str) -> dict:
    return json.loads(subprocess.check_output(["docker", "inspect", name]))[0]


def canonical_mounts(items: list[dict]) -> list[dict]:
    """Treat Docker Desktop's documented host-drive alias as the same source."""
    rows = []
    for item in items:
        row = dict(item)
        if row["Type"] == "bind":
            source = row["Source"].replace("\\", "/")
            match = re.fullmatch(r"([A-Za-z]):/(.*)", source)
            if match:
                source = "/run/desktop/mnt/host/" + match[1].lower() + "/" + match[2]
            row["Source"] = source
        rows.append(row)
    return sorted(rows, key=lambda item: item["Destination"])


def actual_uvicorn() -> list[dict]:
    return json.loads(subprocess.check_output(["docker", "exec", "smartcs-api", "python", "-c", PROC_CODE]))


def main() -> int:
    old = inspect("smartcs-api")
    assert old["Image"] == PLAN["old_image_id"], "Expected original CPU image before switching"
    # Test inspection code BEFORE touching the service. bytes([0]) is deliberate:
    # Windows CreateProcess rejects an embedded NUL in a command-line argument.
    assert "\x00" not in PROC_CODE
    assert len(actual_uvicorn()) == 1
    others = {name: inspect(name)["Id"] for name in (
        "smartcs-pi-harness", "smartcs-redis", "smartcs-checkpoint-mysql",
    )}
    env = os.environ.copy()
    env.update(SMARTCS_GPU_IMAGE=PLAN["gpu_tag"], SMARTCS_RERANK_MAX_CHARS="0")
    files = ["-f", "compose.yaml", "-f", "compose.gpu.yaml", "-f", PLAN["private_preserve_config"]]
    started = datetime.now(timezone.utc).isoformat()
    start_clock = time.monotonic()
    attempts: list[dict] = []
    try:
        with (OUT / "switch_gpu.log").open("w", encoding="utf-8") as log:
            subprocess.run(["docker", "compose", *files, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "smartcs-api"],
                           env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=True)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            state = inspect("smartcs-api")
            try:
                health = httpx.get("http://127.0.0.1:8001/health", timeout=3, trust_env=False)
                ready = httpx.get("http://127.0.0.1:8971/ready", timeout=5, trust_env=False)
                ok = (health.status_code == 200 and ready.status_code == 200
                      and ready.json().get("ready") is True
                      and state["State"].get("Health", {}).get("Status") == "healthy")
                status = health.status_code
            except httpx.HTTPError:
                status, ok = None, False
            attempts.append({"elapsed_seconds": time.monotonic() - start_clock,
                             "container_health": state["State"].get("Health", {}).get("Status"),
                             "http_status": status})
            if ok:
                break
            time.sleep(5)
        else:
            raise RuntimeError("GPU API did not become healthy within 300s")

        logs = subprocess.check_output(["docker", "logs", "--since", started, "smartcs-api"],
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        safe = [line for line in logs.splitlines() if "RAG model startup" in line or "RAG runtime:" in line]
        assert any("model=BAAI/bge-reranker-v2-m3 device=cuda:0 dtype=torch.float32" in line
                   and "verification=passed" in line for line in safe), "Live-process CUDA weights not proven"
        assert any("model=BAAI/bge-m3 device=cpu dtype=torch.float32" in line
                   and "verification=passed" in line for line in safe), "CPU embedding not proven"
        (OUT / "live_model_startup.txt").write_text("\n".join(safe) + "\n", encoding="utf-8")
        processes = actual_uvicorn()
        assert len(processes) == 1
        pids = {int(re.search(r"pid=(\d+)", line).group(1)) for line in safe if "RAG model startup" in line}
        assert pids == {processes[0]["pid"]}, "Model logs do not belong to the serving uvicorn process"
        new = inspect("smartcs-api")
        new_env = dict(item.split("=", 1) for item in new["Config"]["Env"])
        old_env = dict(item.split("=", 1) for item in old["Config"]["Env"])
        changed = {key for key in old_env.keys() | new_env.keys() if old_env.get(key) != new_env.get(key)}
        assert changed == set(PLAN["changed_environment_keys"]), "Unexpected environment drift"
        # The NVIDIA/Desktop hook may expose a Windows D:\\ path as its
        # /run/desktop/mnt/host/d alias. Keep RAW evidence and compare exactly
        # after that one explicit alias normalization; no other fields ignored.
        mounts_before = canonical_mounts(old["Mounts"])
        mounts_after = canonical_mounts(new["Mounts"])
        (OUT / "mount_comparison.json").write_text(json.dumps({"before_raw": old["Mounts"], "after_raw": new["Mounts"],
            "before_canonical": mounts_before, "after_canonical": mounts_after,
            "equal_logical_definitions": mounts_before == mounts_after}, indent=2), encoding="utf-8")
        assert mounts_before == mounts_after, "Logical mount definitions changed"
        assert new["HostConfig"]["PortBindings"] == old["HostConfig"]["PortBindings"], "Ports changed"
        assert new["Config"]["Cmd"] == old["Config"]["Cmd"] and new["Config"]["User"] == old["Config"]["User"]
        assert all(inspect(name)["Id"] == cid for name, cid in others.items()), "Other service restarted"
        result = {
            "started_utc": started, "completed_utc": datetime.now(timezone.utc).isoformat(),
            "image_id": new["Image"], "image_tag": new["Config"]["Image"], "api_container_id": new["Id"],
            "health": new["State"]["Health"]["Status"], "device_requests": new["HostConfig"]["DeviceRequests"],
            "actual_uvicorn_processes": processes, "safe_model_logs": safe,
            "environment_changed_keys": sorted(changed), "mounts_unchanged": True, "ports_unchanged": True,
            "other_container_ids_unchanged": others, "readiness": ready.json(),
            "health_poll_attempts": attempts, "rollback_needed": False,
        }
        (OUT / "deployment_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except BaseException as exc:
        (OUT / "deployment_failure.json").write_text(json.dumps({"error_type": type(exc).__name__, "error": str(exc),
            "health_poll_attempts": attempts, "rollback_started": True}, indent=2), encoding="utf-8")
        rollback_env = os.environ.copy()
        rollback_env["SMARTCS_IMAGE"] = PLAN["rollback_tag"]
        with (OUT / "rollback.log").open("w", encoding="utf-8") as log:
            subprocess.run(["docker", "compose", "-f", "compose.yaml", "-f", PLAN["private_preserve_config"],
                "up", "-d", "--no-deps", "--no-build", "--pull", "never", "smartcs-api"],
                env=rollback_env, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
