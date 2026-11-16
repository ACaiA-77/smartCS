"""One real authenticated, fresh-session knowledge Q&A; never persist credentials."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
QUESTION = (
    "请只调用一次 knowledge_search，query 为“Apple 账户恢复等待时间，联系客服能否缩短等待”，"
    "top_k 为 3。随后根据检索内容，用150字以内回答这个问题，并附官方来源链接。"
    "只做知识查询，不执行其他工具或任何业务写操作。"
)

TRANSCRIPT_SUMMARY_JS = r"""
const fs = require('node:fs');
const path = require('node:path');
const sid = process.argv[1];
const root = process.env.SMARTCS_PI_SESSION_DIR || '/app/.runtime/pi-sessions';
function files(dir) {
  return fs.readdirSync(dir, {withFileTypes: true}).flatMap(e =>
    e.isDirectory() ? files(path.join(dir,e.name)) :
    e.name.endsWith('.jsonl') ? [path.join(dir,e.name)] : []);
}
const matches = files(root).filter(p => {
  try { return JSON.parse(fs.readFileSync(p,'utf8').split('\n')[0]).id === sid; }
  catch { return false; }
});
if (matches.length !== 1) throw new Error('Cannot find one transcript for requested session');
const entries = fs.readFileSync(matches[0],'utf8').split('\n').filter(Boolean).map(JSON.parse);
const starts = new Map();
const model = [], tools = [];
function ids(value) {
  const out = new Set();
  function visit(v) {
    if (Array.isArray(v)) { for (const x of v) visit(x); }
    else if (v && typeof v === 'object') {
      if (typeof v.chunk_id === 'string') out.add(v.chunk_id);
      for (const x of Object.values(v)) visit(x);
    }
  }
  visit(value); return [...out];
}
for (const e of entries) {
  const m = e.message;
  if (!m) continue;
  if (m.role === 'assistant') {
    const calls = Array.isArray(m.content) ? m.content.filter(c => c.type === 'toolCall') : [];
    model.push({entry_at:e.timestamp,message_at:m.timestamp,model:m.model,provider:m.provider,
      stop_reason:m.stopReason,usage:m.usage,tool_names:calls.map(c=>c.name)});
    for (const c of calls) starts.set(c.id,{entry_at:e.timestamp,name:c.name,args:c.arguments});
  }
  if (m.role === 'toolResult') {
    const call=starts.get(m.toolCallId);
    tools.push({name:m.toolName,call_id:m.toolCallId,is_error:m.isError===true,
      call_entry_at:call?.entry_at,result_entry_at:e.timestamp,args:call?.args,
      wall_ms_approx:call ? Date.parse(e.timestamp)-Date.parse(call.entry_at) : null,
      result_chunk_ids:ids(m.details),
      detail_keys:m.details && typeof m.details==='object' ? Object.keys(m.details) : [],
      content_preview:Array.isArray(m.content) ? m.content.filter(c=>c.type==='text').map(c=>c.text).join('\n').slice(0,350) : null});
  }
}
console.log(JSON.stringify({session_id:sid,transcript_file:matches[0],models:model,tools,
  limitation:'Tool wall time uses transcript entry timestamps, not an instrumented start/end event.'}));
"""


def credentials(use_report: bool) -> tuple[str, str]:
    user, password = os.getenv("SMARTCS_QA_USERNAME"), os.getenv("SMARTCS_QA_PASSWORD")
    if user and password:
        return user, password
    if use_report:
        text = (ROOT / "pi-harness/PHASE9_REPORT.md").read_text(encoding="utf-8")
        match = re.search(r"用户名 `([^`]+)`.*密码 `([^`]+)`", text)
        if match:
            return match.group(1), match.group(2)
    raise RuntimeError("Supply existing test-account credentials or explicitly select --demo-from-report")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--label", required=True)
    parser.add_argument("--session-base", help="Optional existing Pi-cohort public entry for session creation only; both entries require legitimate login")
    parser.add_argument("--demo-from-report", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        parser.error("invalid label")
    user, password = credentials(args.demo_from_report)
    base = args.api_base.rstrip("/")
    request_id = f"gpuqa-{args.label}-{uuid.uuid4().hex[:16]}"
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "api_base": base, "label": args.label, "question": QUESTION,
        "client_request_id": request_id, "auth": "real Argon2 login + HttpOnly cookie; token not saved",
        "credentials_source": "environment or existing documented demo account",
    }
    with httpx.Client(base_url=base, headers={"Origin": base}, timeout=240, trust_env=False) as client:
        response = client.post("/api/auth/login", json={"username": user, "password": password})
        response.raise_for_status()
        response = client.get("/api/auth/me")
        response.raise_for_status()
        own_account = response.json()["account_id"]
        session_base = (args.session_base or base).rstrip("/")
        if session_base != base:
            with httpx.Client(base_url=session_base, headers={"Origin":session_base}, timeout=30, trust_env=False) as creator:
                login = creator.post("/api/auth/login", json={"username":user,"password":password})
                login.raise_for_status()
                identity = creator.get("/api/auth/me")
                identity.raise_for_status()
                if identity.json()["account_id"] != own_account:
                    raise RuntimeError("The two public entries authenticated different accounts")
                response = creator.post("/api/sessions")
                response.raise_for_status()
                session = response.json()
            # Verify through the real chat entry that the server considers it ours.
            sessions = client.get("/api/sessions")
            sessions.raise_for_status()
            if not any(row["session_id"] == session["session_id"] for row in sessions.json()["sessions"]):
                raise RuntimeError("Created session is not owned according to the chat entry")
        else:
            response = client.post("/api/sessions")
            response.raise_for_status()
            session = response.json()
        report["session_creation_api_base"] = session_base
        report["session_id"] = session["session_id"]
        report["session_harness_version"] = session.get("harness_version")
        if session.get("harness_version") != "pi":
            raise RuntimeError("The selected public entry did not create a Pi session; do not alter rollout silently")
        started = time.perf_counter()
        response = client.post("/api/chat", json={
            "message": QUESTION, "session_id": session["session_id"], "client_request_id": request_id,
        })
        report["chat_wall_ms"] = (time.perf_counter() - started) * 1000
        report["http_status"] = response.status_code
        report["response"] = response.json()
    (OUT / f"{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if response.status_code != 200:
        print(json.dumps({"label":args.label,"status":response.status_code,"wall_ms":report["chat_wall_ms"]}))
        return 2
    transcript = subprocess.check_output([
        "docker", "exec", "smartcs-pi-harness", "node", "-e", TRANSCRIPT_SUMMARY_JS, report["session_id"],
    ], text=True, encoding="utf-8", timeout=25)
    evidence = json.loads(transcript)
    (OUT / f"{args.label}_transcript_summary.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    knowledge = [row for row in evidence["tools"] if row["name"] == "knowledge_search"]
    valid = (len(knowledge) == 1 and not knowledge[0]["is_error"]
             and report["response"].get("harness_version") == "pi"
             and all(row["name"] == "knowledge_search" for row in evidence["tools"]))
    report["acceptance"] = {"one_successful_knowledge_tool_no_other_tools": valid}
    (OUT / f"{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"label":args.label,"chat_seconds":report["chat_wall_ms"]/1000,
                      "accepted":valid,"knowledge_tool":knowledge,
                      "answer":report["response"].get("response")}, ensure_ascii=False, indent=2))
    return 0 if valid else 3


if __name__ == "__main__":
    raise SystemExit(main())
