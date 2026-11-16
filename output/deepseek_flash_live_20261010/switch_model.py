"""Credential-private runtime model switch, with rollback; no application code changes."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
PRIVATE = ROOT.parent / ".runtime/deepseek_flash_switch_20261010"
PLAN = json.loads((OUT / "switch_plan.json").read_text())
NAMES = ("smartcs-api", "smartcs-pi-harness")
NODE_PROBE = """import('./src/config/env.ts').then(m=>{const c=m.loadLlmConfigFromPythonEnv();
console.log(JSON.stringify({model:c.model,base_url:c.baseUrl,key_present:Boolean(c.apiKey)}));})"""
spec = importlib.util.spec_from_file_location("gpu_helpers", ROOT / "output/gpu_live_deploy_20261010/deploy_api.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def model_metadata() -> dict:
    return json.loads(subprocess.check_output(["docker", "exec", "smartcs-pi-harness", "node", "--import", "tsx", "-e", NODE_PROBE]))


def update_runtime_files(cfg: dict[str, str]) -> None:
    for filename in (".env", ".env.docker"):
        path = ROOT / filename
        text = path.read_text(encoding="utf-8")
        for key, value in cfg.items():
            pattern = re.compile(r"(?m)^" + re.escape(key) + r"=.*$")
            assert len(pattern.findall(text)) == 1
            text = pattern.sub(lambda _: key + "=" + value, text)
        # Existing bind mount must still see the same file.
        with path.open("r+", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.truncate()


def main() -> int:
    # Prove this container's TSX module path BEFORE changing the service.
    before_metadata = model_metadata()
    assert before_metadata["model"] == "kimi-k2.7-code", "This one-off switch requires the original Kimi deployment; refuse to repeat it on DeepSeek"
    old = {name: helpers.inspect(name) for name in NAMES}
    stable = {name: helpers.inspect(name)["Id"] for name in ("smartcs-redis", "smartcs-checkpoint-mysql")}
    cfg = dict(line.split("=", 1) for line in (PRIVATE / ".env.deepseek-credential").read_text().splitlines() if "=" in line)
    env = os.environ.copy()
    env.update(SMARTCS_GPU_IMAGE="smartcs-api:gpu-fp32-20261010", SMARTCS_RERANK_MAX_CHARS="0")
    started = datetime.now(timezone.utc).isoformat()
    attempts = []
    try:
        update_runtime_files(cfg)
        with (OUT / "switch_model.log").open("w", encoding="utf-8") as log:
            subprocess.run(["docker", "compose", "-f", "compose.yaml", "-f", "compose.gpu.yaml", "-f", PLAN["private_override_path"],
                            "up", "-d", "--no-deps", "--no-build", "--pull", "never", *NAMES],
                           env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=True)
        start = time.monotonic()
        while time.monotonic() - start < 300:
            new = {name: helpers.inspect(name) for name in NAMES}
            try:
                health = httpx.get("http://127.0.0.1:8001/health", timeout=3, trust_env=False)
                ready = httpx.get("http://127.0.0.1:8971/ready", timeout=5, trust_env=False)
                ok = (health.status_code == 200 and ready.status_code == 200 and ready.json().get("ready") is True
                      and all(state["State"].get("Health", {}).get("Status") == "healthy" for state in new.values()))
            except httpx.HTTPError:
                ok = False
            attempts.append({"elapsed_seconds": time.monotonic() - start, "healthy": ok})
            if ok:
                break
            time.sleep(5)
        else:
            raise RuntimeError("Model-switch services did not become ready")
        for name, state in new.items():
            before = dict(value.split("=", 1) for value in old[name]["Config"]["Env"])
            after = dict(value.split("=", 1) for value in state["Config"]["Env"])
            delta = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
            assert delta == set(cfg), "Unexpected environment drift"
            assert state["Image"] == old[name]["Image"], "Image changed"
            assert helpers.canonical_mounts(state["Mounts"]) == helpers.canonical_mounts(old[name]["Mounts"])
            assert state["HostConfig"]["PortBindings"] == old[name]["HostConfig"]["PortBindings"]
        assert new["smartcs-api"]["HostConfig"]["DeviceRequests"] == old["smartcs-api"]["HostConfig"]["DeviceRequests"]
        assert all(helpers.inspect(name)["Id"] == cid for name, cid in stable.items())
        metadata = model_metadata()
        assert metadata["model"] == cfg["MODEL_NAME"] and metadata["base_url"] == cfg["OPENAI_BASE_URL"]
        logs = subprocess.check_output(["docker", "logs", "--since", started, "smartcs-api"], stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8")
        safe = [line for line in logs.splitlines() if "RAG model startup" in line or "RAG runtime:" in line]
        assert any("bge-reranker-v2-m3 device=cuda:0 dtype=torch.float32" in line for line in safe)
        assert any("bge-m3 device=cpu dtype=torch.float32" in line for line in safe)
        report = {"started_utc": started, "completed_utc": datetime.now(timezone.utc).isoformat(),
                  "model_metadata": metadata, "images_unchanged": True, "changed_keys": PLAN["changed_keys"],
                  "gpu_device_requests_unchanged": True, "logical_mounts_unchanged": True, "ports_unchanged": True,
                  "database_and_redis_ids_unchanged": True, "safe_model_startup_logs": safe,
                  "ready": ready.json(), "health_poll_attempts": attempts}
        (OUT / "deployment_result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except BaseException as exc:
        for filename in (".env", ".env.docker"):
            target = ROOT / filename
            saved = (PRIVATE / (".env.backup-" + filename.lstrip("."))).read_text(encoding="utf-8")
            with target.open("r+", encoding="utf-8", newline="") as handle:
                handle.write(saved)
                handle.truncate()
        with (OUT / "rollback_model.log").open("w", encoding="utf-8") as log:
            subprocess.run(["docker", "compose", "-f", "compose.yaml", "-f", "compose.gpu.yaml", "-f", PLAN["private_rollback_path"],
                            "up", "-d", "--no-deps", "--no-build", "--pull", "never", *NAMES],
                           env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=True)
        # Never persist exception text that may contain transport credentials.
        (OUT / "deployment_failure.json").write_text(json.dumps({"error_type": type(exc).__name__, "rollback_initiated": True}), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
