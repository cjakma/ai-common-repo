"""claudecode-memory pre|post — hooks for the EXISTING ClaudeCode wrapper.

Never patches the ClaudeCode binary or repo CLAUDE.md (design §4.3 / §16).
`pre`  writes a context block to --out-context for the wrapper to inject.
`post` ingests the finished session from wrapper-provided logs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ai_common_repo.client import GatewayClient
from ai_common_repo.core.config import load_config
from ai_common_repo.project_scope.detector import detect_git_info, detect_project_id


def _gateway() -> GatewayClient:
    cfg = load_config()
    return GatewayClient(f"http://{cfg.server.host}:{cfg.server.port}")


def _read(path: str | None) -> str:
    if path and os.path.isfile(path):
        return open(path, encoding="utf-8", errors="replace").read()
    return ""


def cmd_pre(args) -> int:
    cwd = args.cwd or os.getcwd()
    query = ""
    if args.argv_json:
        try:
            query = " ".join(json.loads(args.argv_json))
        except json.JSONDecodeError:
            query = args.argv_json
    block = _gateway().context_block(query=query, project_id=detect_project_id(cwd),
                                     tool="claudecode", cwd=cwd)
    if args.out_context:
        open(args.out_context, "w", encoding="utf-8").write(block)
    else:
        print(block)
    return 0


def cmd_post(args) -> int:
    cwd = args.cwd or os.getcwd()
    messages = []
    stdin_text = _read(args.stdout)
    if stdin_text:
        messages.append({"role": "assistant", "content": stdin_text})
    _gateway().ingest_session({
        "schema": "ai-common-repo.session.v1",
        "tool": "claudecode", "wrapper": True,
        "session_id": args.session_id, "project_id": detect_project_id(cwd), "cwd": cwd,
        "git": detect_git_info(cwd),
        "messages": messages,
        "artifacts": {"exit_code": args.exit_code,
                      "diff": _read(args.diff)[:4000]},
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claudecode-memory")
    sub = p.add_subparsers(dest="cmd", required=True)
    pre = sub.add_parser("pre")
    pre.add_argument("--cwd"); pre.add_argument("--argv-json"); pre.add_argument("--out-context")
    pre.set_defaults(func=cmd_pre)
    post = sub.add_parser("post")
    post.add_argument("--cwd"); post.add_argument("--session-id")
    post.add_argument("--exit-code", type=int); post.add_argument("--stdout")
    post.add_argument("--stderr"); post.add_argument("--diff")
    post.set_defaults(func=cmd_post)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
