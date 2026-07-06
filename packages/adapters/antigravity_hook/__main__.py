"""antigravity-memory pre|post — hooks for the EXISTING Antigravity-CLI wrapper.

Adds multi-agent run metadata to the session envelope (design §4.4). Avoids
double-wrapping; long logs are summarized/truncated before ingest.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ai_common_repo.client import GatewayClient
from ai_common_repo.core.config import load_config
from ai_common_repo.project_scope.detector import detect_git_info, detect_project_id

_LOG_MAX = 8000  # raw log truncation before ingest


def _gateway() -> GatewayClient:
    cfg = load_config()
    return GatewayClient(f"http://{cfg.server.host}:{cfg.server.port}")


def _read(path: str | None) -> str:
    if path and os.path.isfile(path):
        return open(path, encoding="utf-8", errors="replace").read()
    return ""


def cmd_pre(args) -> int:
    cwd = args.cwd or os.getcwd()
    block = _gateway().context_block(query=args.task or "", tool="antigravity-cli",
                                     project_id=detect_project_id(cwd), cwd=cwd)
    if args.out_context:
        open(args.out_context, "w", encoding="utf-8").write(block)
    else:
        print(block)
    return 0


def cmd_post(args) -> int:
    cwd = args.cwd or os.getcwd()
    artifacts = {"exit_code": args.exit_code, "log_tail": _read(args.log)[-_LOG_MAX:],
                 "diff": _read(args.diff)[:4000]}
    if args.artifacts:
        try:
            artifacts["artifacts"] = json.loads(args.artifacts)
        except json.JSONDecodeError:
            pass
    _gateway().ingest_session({
        "schema": "ai-common-repo.session.v1",
        "tool": "antigravity-cli", "wrapper": True,
        "session_id": args.run_id, "project_id": detect_project_id(cwd), "cwd": cwd,
        "git": detect_git_info(cwd),
        "messages": [{"role": "user", "content": args.task or ""}],
        "artifacts": {**artifacts, "run_id": args.run_id, "task_id": args.task_id,
                      "agent_id": args.agent_id, "parent_run_id": args.parent_run_id,
                      "delegation_depth": args.delegation_depth,
                      "worktree_path": args.worktree_path},
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="antigravity-memory")
    sub = p.add_subparsers(dest="cmd", required=True)
    pre = sub.add_parser("pre")
    pre.add_argument("--run-id")
    pre.add_argument("--task")
    pre.add_argument("--cwd")
    pre.add_argument("--out-context")
    pre.set_defaults(func=cmd_pre)
    post = sub.add_parser("post")
    post.add_argument("--run-id")
    post.add_argument("--task")
    post.add_argument("--cwd")
    post.add_argument("--exit-code", type=int)
    post.add_argument("--log")
    post.add_argument("--diff")
    post.add_argument("--artifacts")
    post.add_argument("--task-id")
    post.add_argument("--agent-id")
    post.add_argument("--parent-run-id")
    post.add_argument("--delegation-depth", type=int)
    post.add_argument("--worktree-path")
    post.set_defaults(func=cmd_post)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
