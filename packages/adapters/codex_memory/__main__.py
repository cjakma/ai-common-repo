"""codex-memory — prompt-prefix adapter (design §4.2 / §16).

Flow: detect project -> recall context -> prepend as a system/context prefix to
the codex prompt -> exec real codex -> capture envelope -> ingest session.

Thin by design: it never edits codex internals and fails open (runs codex even
if the gateway is down).
"""
from __future__ import annotations

import os
import subprocess
import sys

from ai_common_repo.client import GatewayClient
from ai_common_repo.core.config import load_config
from ai_common_repo.project_scope.detector import detect_git_info, detect_project_id

_CODEX_BIN = os.environ.get("CODEX_BIN", "codex")


def _gateway() -> GatewayClient:
    cfg = load_config()
    return GatewayClient(f"http://{cfg.server.host}:{cfg.server.port}")


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    if argv and argv[0] == "--store":
        _gateway().store(type="decision", scope="project",
                         project_id=detect_project_id(os.getcwd()),
                         content=" ".join(argv[1:]))
        return 0
    if argv and argv[0] == "--recall":
        print(_gateway().context_block(query=" ".join(argv[1:]),
                                       project_id=detect_project_id(os.getcwd()),
                                       tool="codex", cwd=os.getcwd()))
        return 0

    no_memory = "--no-memory" in argv
    argv = [a for a in argv if a != "--no-memory"]
    cwd = os.getcwd()
    project_id = detect_project_id(cwd)
    prompt = argv[-1] if argv else ""

    if not no_memory and prompt:
        block = _gateway().context_block(query=prompt, project_id=project_id,
                                         tool="codex", cwd=cwd)
        if block:
            argv[-1] = f"{block}\n\n--- TASK ---\n{prompt}"

    proc = subprocess.run([_CODEX_BIN, *argv], capture_output=False)

    if not no_memory and prompt:
        _gateway().ingest_session({
            "schema": "ai-common-repo.session.v1",
            "tool": "codex", "wrapper": True, "project_id": project_id, "cwd": cwd,
            "git": detect_git_info(cwd),
            "messages": [{"role": "user", "content": prompt}],
            "artifacts": {"exit_code": proc.returncode},
        })
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
