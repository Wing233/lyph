import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import sys
from . import security


def _init_config(args) -> None:
    target = Path(args.output)
    if target.exists():
        raise SystemExit("Configuration already exists; refusing to overwrite secrets")
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Owner password: ")
    if len(password) < 12:
        raise SystemExit("Owner password must contain at least 12 characters")
    if not args.password_stdin and getpass.getpass("Repeat owner password: ") != password:
        raise SystemExit("Passwords do not match")
    values = {
        "LYPH_SECRET_KEY": secrets.token_urlsafe(48),
        "LYPH_OWNER_PASSWORD_HASH": security.hash_password(password),
        "LYPH_DATA_DIR": args.data_dir,
        "LYPH_FRONTEND_ORIGIN": args.origin.rstrip("/"),
        "LYPH_SECURE_COOKIES": "true" if args.production else "false",
        "LYPH_MINIO_ENDPOINT": args.minio_endpoint,
        "LYPH_MINIO_ACCESS_KEY": "lyph-" + secrets.token_hex(12),
        "LYPH_MINIO_SECRET_KEY": secrets.token_urlsafe(48),
        "LYPH_MINIO_BUCKET": "lyph-private",
        "LYPH_MINIO_SECURE": "false",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("# Generated locally. Never commit this file.\n")
        for key, value in values.items():
            if "'" in value or "\n" in value:
                raise SystemExit("Invalid configuration value")
            stream.write(f"{key}='{value}'\n")
    print(f"Configuration created: {target.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="lyph")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-config", help="Create secrets and owner Argon2id hash without displaying them")
    init.add_argument("--output", default=".env")
    init.add_argument("--password-stdin", action="store_true")
    init.add_argument("--production", action="store_true")
    init.add_argument("--data-dir", default="./data")
    init.add_argument("--origin", default="http://localhost:3000")
    init.add_argument("--minio-endpoint", default="localhost:9000")
    sub.add_parser("init-db")
    sub.add_parser("backup")
    restore = sub.add_parser("restore", help="Offline restore: stop the API and task worker first")
    restore.add_argument("path", type=Path)
    sub.add_parser("cleanup")
    pause = sub.add_parser("pause-heavy", help="Pause new heavy-task claims until the deadline; running tasks continue")
    pause.add_argument("--seconds", type=int, default=1800)
    sub.add_parser("resume-heavy", help="Clear the heavy-task pause marker; safe to repeat")
    args = parser.parse_args()
    if args.command == "init-config":
        _init_config(args)
        return
    if args.command in {"pause-heavy", "resume-heavy"}:
        # Online queue coordination must not initialize/recover application or chat state.
        from . import tasks
        try:
            result = tasks.pause_heavy(args.seconds) if args.command == "pause-heavy" else tasks.resume_heavy()
        except (ValueError, OSError) as exc:
            raise SystemExit(f"重任务队列操作失败：{exc}") from None
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command in {"backup", "cleanup"} and os.getenv("LYPH_MAINTENANCE_OFFLINE") != "1":
        raise SystemExit("在线维护请通过后台任务执行；停 API 后设置 LYPH_MAINTENANCE_OFFLINE=1 才能执行 CLI backup/cleanup")
    if args.command == "restore" and os.getenv("LYPH_RESTORE_OFFLINE") != "1":
        raise SystemExit("停 API 后设置 LYPH_RESTORE_OFFLINE=1 才能恢复备份")
    from .main import init_all
    init_all(recover_requests=False)
    if args.command == "init-db":
        print("Database migrations applied; no demonstration records inserted")
        return
    from . import maintenance
    if args.command == "backup":
        result = maintenance.backup()
    elif args.command == "restore":
        result = maintenance.restore_backup(args.path)
    else:
        result = maintenance.cleanup()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
