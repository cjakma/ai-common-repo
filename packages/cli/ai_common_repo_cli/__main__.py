"""ai-common-repo CLI. Talks to the local MemoryService directly (no server
required) except `serve`. Output is JSON for easy piping."""
from __future__ import annotations

import argparse
import json
import sys

from ai_common_repo.core.config import load_config
from ai_common_repo.core.models import (
    DocumentIngestRequest, RecallRequest, SearchRequest, SessionIngestRequest,
    StoreMemoryRequest,
)
from ai_common_repo.project_scope.detector import detect_project_id
from ai_common_repo.service import MemoryService


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _service(args) -> MemoryService:
    return MemoryService(load_config(getattr(args, "config", None)))


def cmd_serve(args) -> int:
    from ai_common_repo.server.app import run
    run(load_config(args.config), host=args.host, port=args.port)
    return 0


def cmd_status(args) -> int:
    svc = _service(args)
    _print({"config_db": str(svc.cfg.storage.db_file),
            "default_user": svc.cfg.identity.default_user_id,
            "backends": svc.backend_health()})
    return 0


def cmd_recall(args) -> int:
    svc = _service(args)
    resp = svc.recall(RecallRequest(query=args.query, project_id=args.project_id,
                                    user_id=args.user_id))
    _print(resp.model_dump())
    return 0


def cmd_context(args) -> int:
    svc = _service(args)
    project_id = args.project_id or detect_project_id(args.cwd)
    resp = svc.recall(RecallRequest(query=args.query or "", project_id=project_id,
                                    tool=args.tool, cwd=args.cwd))
    print(resp.context_block)
    return 0


def cmd_store(args) -> int:
    svc = _service(args)
    mem = svc.store_memory(StoreMemoryRequest(
        type=args.type, scope=args.scope, content=args.content,
        project_id=args.project_id, tags=args.tag or []))
    if mem is None:
        _print({"stored": False, "deduped": True})
    else:
        _print({"stored": True, "id": mem.id})
    return 0


def cmd_search(args) -> int:
    svc = _service(args)
    _print(svc.search(SearchRequest(q=args.query, project_id=args.project_id,
                                    limit=args.limit, search_mode=args.mode)))
    return 0


def cmd_ingest_doc(args) -> int:
    svc = _service(args)
    content = None
    if not args.path:
        content = sys.stdin.read() if args.file == "-" else \
            open(args.file, encoding="utf-8").read()
    out = svc.ingest_document(DocumentIngestRequest(
        content=content, path=args.path, project_id=args.project_id,
        user_id=args.user_id, source_type=args.source_type,
        source_uri=args.source_uri, title=args.title))
    _print(out)
    return 0


def cmd_profile(args) -> int:
    svc = _service(args)
    p = svc.get_profile(args.user_id, args.project_id)
    _print(p.model_dump())
    return 0


def cmd_forget(args) -> int:
    _service(args).forget(args.memory_id)
    _print({"forgotten": True, "id": args.memory_id})
    return 0


def cmd_ingest(args) -> int:
    svc = _service(args)
    data = json.load(open(args.file, encoding="utf-8"))
    _print(svc.ingest_session(SessionIngestRequest.model_validate(data)))
    return 0


def cmd_project_id(args) -> int:
    print(detect_project_id(args.cwd))
    return 0


def cmd_backends(args) -> int:
    _print(_service(args).backend_health())
    return 0


def cmd_worker(args) -> int:
    """Run the background worker + scheduler until SIGTERM/SIGINT.

    Decoupled from `serve` so jobs can be processed by a separate process while
    the HTTP gateway stays request-only."""
    import signal
    import threading

    from ai_common_repo.workers.runtime import build_runtime

    svc = _service(args)
    worker, scheduler = build_runtime(svc, svc.cfg)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    worker.start()
    scheduler.start()
    _print({"worker": "started", "db": str(svc.cfg.storage.db_file)})
    try:
        stop.wait()
    finally:
        scheduler.stop()
        worker.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai-common-repo")
    p.add_argument("--config", help="extra config yaml (highest precedence)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve"); s.add_argument("--host"); s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    sub.add_parser("status").set_defaults(func=cmd_status)

    r = sub.add_parser("recall"); r.add_argument("query")
    r.add_argument("--project-id"); r.add_argument("--user-id")
    r.set_defaults(func=cmd_recall)

    c = sub.add_parser("context"); c.add_argument("--query", default="")
    c.add_argument("--tool"); c.add_argument("--cwd"); c.add_argument("--project-id")
    c.set_defaults(func=cmd_context)

    st = sub.add_parser("store"); st.add_argument("content")
    st.add_argument("--type", default="decision"); st.add_argument("--scope", default="project")
    st.add_argument("--project-id"); st.add_argument("--tag", action="append")
    st.set_defaults(func=cmd_store)

    se = sub.add_parser("search"); se.add_argument("query")
    se.add_argument("--project-id"); se.add_argument("--limit", type=int, default=20)
    se.add_argument("--mode", choices=["fts", "vector", "hybrid"], default="fts")
    se.set_defaults(func=cmd_search)

    idoc = sub.add_parser("ingest-doc")
    src = idoc.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="read content from a file ('-' for stdin)")
    src.add_argument("--path", help="server-side path to ingest (allowed_roots)")
    idoc.add_argument("--project-id")
    idoc.add_argument("--user-id")
    idoc.add_argument("--source-type", default="text")
    idoc.add_argument("--source-uri")
    idoc.add_argument("--title")
    idoc.set_defaults(func=cmd_ingest_doc)

    pr = sub.add_parser("profile"); pr.add_argument("--user-id"); pr.add_argument("--project-id")
    pr.set_defaults(func=cmd_profile)

    f = sub.add_parser("forget"); f.add_argument("memory_id"); f.set_defaults(func=cmd_forget)

    ig = sub.add_parser("ingest-session"); ig.add_argument("file"); ig.set_defaults(func=cmd_ingest)

    pid = sub.add_parser("project-id"); pid.add_argument("action", choices=["detect"])
    pid.add_argument("--cwd"); pid.set_defaults(func=cmd_project_id)

    mb = sub.add_parser("model-backends"); mb.add_argument("action", choices=["health"])
    mb.set_defaults(func=cmd_backends)

    sub.add_parser("worker").set_defaults(func=cmd_worker)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
