"""Loopback-only OFM fixture and replay runner. No real settings or services are used."""

import argparse
import base64
import contextlib
import hashlib
import http.client
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time


CASES = Path(__file__).resolve().parent
ROOT = CASES.parent.parent
REFERENCE = ["node", "./rust/app-cases/reference-port.mjs"]
KEY = "fixture-key-中文😀"
COOKIE = "ofm_management_18900"
WATCHED = (
    "host", "cookie", "authorization", "origin", "referer", "content-length",
    "content-type", "transfer-encoding", "connection", "x-custom",
)


class Fixture(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 18900), Upstream)
        self.lock = threading.Lock()
        self.log = []
        self.sessions = 0
        self.retries = 0
        self.session_status = 200
        self.session_headers = None
        self.session_delay = 0
        self.release_chunk = threading.Event()

    def events(self):
        with self.lock:
            return list(self.log)

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.respond
        raise AttributeError(name)

    def log_message(self, *_args):
        pass

    def respond(self):
        chunk = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        event = {
            "method": self.command,
            "target": self.path,
            "headers": {key: self.headers.get_all(key) for key in WATCHED},
            "size": len(chunk),
            "sha256": hashlib.sha256(chunk).hexdigest(),
        }
        with self.server.lock:
            self.server.log.append(event)
            if self.path == "/api/management/session":
                self.server.sessions += 1
                generation = self.server.sessions
            elif self.path == "/retry":
                self.server.retries += 1
                generation = self.server.retries

        path = self.path.split("?", 1)[0]
        if path == "/api/management/session":
            parsed = json.loads(chunk)
            event["key"] = parsed["key"]
            time.sleep(self.server.session_delay)
            headers = self.server.session_headers
            if headers is None:
                headers = [("Set-Cookie", f"{COOKIE}=session-{generation}=token; Path=/")]
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.answer(self.server.session_status, b"session", headers)
        elif path == "/retry":
            self.answer(401 if generation % 2 else 200, b"retry")
        elif path == "/always-401":
            self.answer(401, b"denied", [("WWW-Authenticate", 'Basic realm="fixture"')])
        elif path == "/denied":
            self.answer(403, b"denied")
        elif path == "/missing":
            self.answer(404, b"missing")
        elif path == "/redirect":
            self.answer(302, b"redirect", [("Location", "/destination?a=1")])
        elif path == "/empty":
            self.answer(204)
        elif path == "/unchanged":
            self.answer(304, headers=[("ETag", '"fixture"')])
        elif path == "/range":
            self.answer(206, b"234", [("Content-Range", "bytes 2-4/10")])
        elif path == "/drop":
            self.close_connection = True
        elif path == "/headers":
            self.answer(202, b"headers", [
                ("Set-Cookie", "private=1"), ("Set-Cookie", "private=2"),
                ("Keep-Alive", "timeout=1"), ("X-Duplicate", "one"), ("X-Duplicate", "two"),
                ("ETag", '"first"'), ("ETag", '"second"'),
            ])
        elif path == "/stream":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (b"\x00\xff", b"binary", b"\r\n"):
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        elif path == "/stream-gated":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"5\r\nfirst\r\n")
            self.wfile.flush()
            if self.server.release_chunk.wait(timeout=4):
                self.wfile.write(b"4\r\nlast\r\n0\r\n\r\n")
            else:
                self.close_connection = True
        elif path == "/digest":
            self.answer(200, event["sha256"].encode())
        else:
            # Cookie rotation is checked in the stateful comparison, separately from HTTP replay.
            headers = {k: v for k, v in event["headers"].items() if k != "cookie"}
            body = {
                "method": self.command, "target": self.path,
                "headers": headers, "body": base64.b64encode(chunk).decode(),
            }
            self.answer(200, json.dumps(body, sort_keys=True).encode())

    def answer(self, status, body=b"", headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if status not in (204, 304):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


@contextlib.contextmanager
def upstream():
    with Fixture() as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def settings(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def app_env(path):
    env = os.environ.copy()
    env.update(OFM_SETTINGS_PATH=str(path), OFM_PROXY_ALLOW="127.0.0.1")
    env.pop("OFM_PROXY_PORT", None)
    return env


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextlib.contextmanager
def running(command, settings_path, allow=None):
    port = free_port()
    env = app_env(settings_path)
    env.update(PORT=str(port), OFM_PROXY_PORT=str(port))
    if allow is not None:
        env["OFM_PROXY_ALLOW"] = allow
    with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT) as process:
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError(process.communicate()[0].decode("utf-8", "replace"))
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{command[0]} did not listen")
                    time.sleep(0.02)
            yield port, process
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def exchange(port, method="GET", target="/healthz", headers=(), body=None, *, timeout=5,
             chunked=False, close=True):
    with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)) as conn:
        conn.putrequest(method, target, skip_accept_encoding=True)
        if close:
            conn.putheader("Connection", "close")
        for key, value in headers:
            conn.putheader(key, value)
        if body is not None:
            if chunked:
                conn.putheader("Transfer-Encoding", "chunked")
            else:
                conn.putheader("Content-Length", str(len(body)))
        try:
            conn.endheaders(body, encode_chunked=chunked)
            response = conn.getresponse()
            fields = {}
            for name, value in response.getheaders():
                fields.setdefault(name.lower(), []).append(value)
            # The instant in Date varies between processes; its presence is checked.
            if "date" in fields:
                fields["date"] = ["<date>"] * len(fields["date"])
            return response.status, response.reason, fields, response.read()
        except (ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected,
                BrokenPipeError):
            return "connection closed"


def wire(port, request, *, reuse=False):
    with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as conn:
        if reuse:
            conn.request("GET", "/healthz")
            assert conn.getresponse().read() == b"ok"
        else:
            conn.connect()
        client = conn.sock
        if isinstance(request, bytes):
            client.sendall(request)
        else:
            for index, part in enumerate(request):
                if index:
                    time.sleep(0.025)
                client.sendall(part)
        chunks = []
        while True:
            try:
                chunk = client.recv(8192)
            except ConnectionResetError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    out = b"".join(chunks)
    messages = []
    while out:
        head, delimiter, out = out.partition(b"\r\n\r\n")
        assert delimiter, head
        status, *headers = head.split(b"\r\n")
        fields = {}
        for field in headers:
            name, value = field.split(b":", 1)
            name = name.lower()
            fields.setdefault(name, []).append(b"<date>" if name == b"date" else value.strip())
        messages.append((status, fields))
        if not status.split()[1].startswith(b"1"):
            break
    return messages, out


def streaming(port, fixture):
    with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=2)) as conn:
        conn.request("GET", "/stream-gated")
        answer = conn.getresponse()
        first = answer.read(5)
        fixture.release_chunk.set()
        return first, answer.read()


def http_fixture(command, path):
    settings(path, {"managementKey": KEY, "forwardKey": "ignored"})
    out = {}
    with upstream() as fixture, running(command, path, "\ufeff 127.0.0.1 , ") as (port, _):
        def take(name, value):
            out[name] = value, fixture.events()

        fixture.session_delay = 0.15
        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(lambda _: exchange(port), range(12)))
        assert fixture.sessions == 1
        take("并发健康检查只创建一次会话", responses)
        fixture.session_delay = 0

        take("请求头去除、重写、重复头合并和原样目标", exchange(
            port, "PATCH", "/echo/%E4%B8%AD?x=%2f&x=+",
            [("Origin", "https://mock.invalid"), ("Referer", "https://mock.invalid/page"),
             ("Authorization", "Bearer fixture"), ("Cookie", "client=1"),
             ("X-Custom", "first"), ("x-custom", "second"), ("TE", "trailers"),
             ("Content-Type", "application/octet-stream"), ("Content-Type", "ignored/type")],
            b"\x00\xff\x01",
        ))
        take("HTTP 持久连接响应头", exchange(port, close=False))
        take("绝对形式请求目标", exchange(port, target="http://fixture.invalid/healthz"))
        take("星号形式请求目标", exchange(port, "OPTIONS", "*"))
        take("分块请求重新计算长度", exchange(
            port, "POST", "/digest", body=[b"prefix", b"\0\xff", b"suffix"], chunked=True,
        ))
        take("上游响应头过滤和重复头合并", exchange(port, target="/headers"))
        take("二进制响应", exchange(port, target="/stream"))
        take("首段数据在上游完成前抵达", streaming(port, fixture))

        settings(path, {"managementKey": KEY, "forwardKey": "other"})
        take("有效会话按管理密钥缓存", exchange(port, target="/echo"))
        assert fixture.sessions == 1
        settings(path, {"managementKey": 1, "forwardKey": "fallback"})
        take("密钥变化后重登并使用 forwardKey", exchange(port, target="/echo"))
        assert fixture.sessions == 2
        settings(path, {"managementKey": "", "forwardKey": "fallback"})
        take("空管理密钥回退并复用同钥会话", exchange(port))
        assert fixture.sessions == 2
        take("重登重发保留原始方法与请求体", exchange(
            port, "POST", "/retry", body=b"retry-payload",
        ))
        take("第二次 401 原样返回", exchange(port, target="/always-401"))
        assert fixture.sessions == 4

        path.write_text("{invalid JSON", encoding="utf-8")
        take("缓存存在时仍重新读取损坏配置", (exchange(port), exchange(port, target="/echo")))
        path.unlink()
        take("配置文件缺失", (exchange(port), exchange(port, target="/echo")))
        settings(path, {"managementKey": [], "forwardKey": ""})
        take("缺少可用密钥", (exchange(port), exchange(port, target="/echo")))
        settings(path, {"managementKey": "new"})
        fixture.session_status = 403
        take("会话接口非 200", exchange(port, target="/echo"))
        fixture.session_status = 200
        fixture.session_headers = [("Set-Cookie", "unrelated=1"), ("Set-Cookie", f"{COOKIE}=ok")]
        take("只检查第一条 Set-Cookie", exchange(port, target="/echo"))
        fixture.session_headers = [("Set-Cookie", f"{COOKIE}=; Path=/")]
        take("空会话 Cookie", exchange(port, target="/echo"))
        fixture.session_headers = [("Set-Cookie", f"{COOKIE}=token=a=b; Path=/")]
        take("会话 Cookie 保留多个等号且失败后可恢复", exchange(port, target="/echo"))
        path.write_bytes(b'{"managementKey":"key-\xff"}')
        take("配置 UTF-8 解码替换非法字节", exchange(port, target="/echo"))
        take("上游响应前断连返回 502", exchange(port, target="/drop"))
        assert exchange(port)[0] == 200

        limit = 32 * 1024 * 1024
        data = b"x" * limit
        take("32 MiB 请求完整转发", exchange(port, "POST", "/digest", body=data))
        before = len(fixture.events())
        take("超出一字节关闭连接", exchange(port, "POST", "/digest", body=data + b"x"))
        assert len(fixture.events()) == before

    with upstream() as fixture, running(command, path, "192.0.2.1") as (port, _):
        out["拒绝非白名单对端及伪造转发地址"] = (
            exchange(port, headers=[("X-Forwarded-For", "192.0.2.1")]),
            exchange(port, target="/echo"),
            fixture.events(),
        )
        assert not fixture.events()
    return out


def restart_fixture(command, path):
    settings(path, {"managementKey": KEY})
    with running(command, path) as (port, process):
        with upstream():
            assert exchange(port)[0] == 200
        answer = exchange(port, target="/echo")
        status = process.wait(timeout=4)
        log = process.communicate()[0].decode("utf-8", "replace")
        assert status == 1 and "ECONNREFUSED" in log
        assert "OFM 上游不可达" in log
        return {"已缓存会话且上游拒绝连接时返回 502 并退出 1": (answer, status)}


def protocol_fixture(command, path):
    settings(path, {"managementKey": KEY})
    with upstream(), running(command, path) as (port, _):
        empty = b"Connection: close\r\n\r\n"
        host = b"Host: example.invalid\r\n"
        cases = {
            "缺 Host": b"GET /healthz HTTP/1.1\r\n" + empty,
            "空 Host 与缺 Host 的处理不同": b"GET /healthz HTTP/1.1\r\nHost:\r\n" + empty,
            "缺冒号的请求头": b"GET / HTTP/1.1\r\n" + host + b"Invalid header\r\n" + empty,
            "超长请求头": b"GET / HTTP/1.1\r\n" + host
            + b"X-Large: " + b"x" * 17000 + b"\r\n" + empty,
            "未知 HTTP 方法": b"OTHER / HTTP/1.1\r\n" + host + empty,
            "未知 Expect": b"GET /healthz HTTP/1.1\r\n" + host + b"Expect: example\r\n" + empty,
            "100 Continue 后转发请求体": b"POST /digest HTTP/1.1\r\n" + host
            + b"Expect: 100-continue\r\nContent-Length: 3\r\n" + empty + b"abc",
            "Expect 列表含有 100-continue": b"POST /digest HTTP/1.1\r\n" + host
            + b"Expect: 100-continue, other\r\nContent-Length: 3\r\n" + empty + b"abc",
            "HTTP/1.0 响应版本": b"GET /healthz HTTP/1.0\r\n" + empty,
            "空请求体的临时响应": b"GET /healthz HTTP/1.1\r\n" + host
            + b"Expect: 100-continue\r\nContent-Length: 0\r\n" + empty,
            "健康检查有请求体时也返回临时响应": b"POST /healthz HTTP/1.1\r\n" + host
            + b"Expect: 100-continue\r\nContent-Length: 3\r\n" + empty + b"abc",
        }
        results = {label: wire(port, request) for label, request in cases.items()}
        results["复用长连接时收到未知方法"] = wire(port, cases["未知 HTTP 方法"], reuse=True)
        results["复用长连接时收到缺冒号的请求头"] = wire(
            port, cases["缺冒号的请求头"], reuse=True,
        )
        return results


def diff(binary):
    rust = [str(Path(binary).resolve())]
    with tempfile.TemporaryDirectory(prefix="ofm-diff-") as directory:
        path = Path(directory) / "settings.json"
        reference = http_fixture(REFERENCE, path) | restart_fixture(REFERENCE, path)
        actual = http_fixture(rust, path) | restart_fixture(rust, path)
        reference |= protocol_fixture(REFERENCE, path)
        actual |= protocol_fixture(rust, path)
        reference |= pipeline_fixture(REFERENCE, path)
        actual |= pipeline_fixture(rust, path)
    failures = 0
    for label, expected in reference.items():
        got = actual[label]
        if expected == got:
            print(f"PASS {label}")
        else:
            if isinstance(expected, tuple) and len(expected) == 2 and isinstance(expected[1], list):
                # Show the response and the first divergent upstream call, not the entire history.
                response, events = expected
                got_response, got_events = got
                if response != got_response:
                    print(f"  Node response={response!r}\n  Rust response={got_response!r}")
                for index, (left, right) in enumerate(zip(events, got_events)):
                    if left != right:
                        print(f"  upstream[{index}] Node={left!r}\n  upstream[{index}] Rust={right!r}")
                        break
                if len(events) != len(got_events):
                    print(f"  upstream calls: Node={len(events)}, Rust={len(got_events)}")
            else:
                print(f"  Node={expected!r}\n  Rust={got!r}")
            print(f"DIFF {label}")
            failures += 1
    print(f"{len(reference) - failures}/{len(reference)} comparisons matched")
    if failures:
        raise SystemExit(1)


def bench(binary):
    with tempfile.TemporaryDirectory(prefix="ofm-bench-") as directory:
        path = Path(directory) / "settings.json"
        settings(path, {"managementKey": KEY})
        for name, command in [("Node", REFERENCE), ("Rust", [str(Path(binary).resolve())])]:
            with upstream(), running(command, path) as (port, _):
                for target in ["/healthz", "/digest"]:
                    for _ in range(20):
                        assert exchange(port, target=target)[0] == 200
                    durations = []
                    for _ in range(250):
                        start = time.perf_counter_ns()
                        assert exchange(port, target=target)[0] == 200
                        durations.append((time.perf_counter_ns() - start) / 1_000_000)
                    p95 = sorted(durations)[237]
                    print(f"{name} {target} n=250 p50={statistics.median(durations):.3f} ms p95={p95:.3f} ms")


def live_timeout(binary):
    with tempfile.TemporaryDirectory(prefix="ofm-timeout-") as directory:
        path = Path(directory) / "settings.json"
        settings(path, {"managementKey": KEY})
        for name, command in [("Node", REFERENCE), ("Rust", [str(Path(binary).resolve())])]:
            with upstream() as fixture, running(command, path) as (port, _):
                fixture.session_delay = 30
                start = time.monotonic()
                result = exchange(port, timeout=20)
                elapsed = time.monotonic() - start
                assert result[0] == 503 and result[3] == b"unavailable", result
                assert 14.5 <= elapsed <= 17.5, elapsed
                fixture.session_delay = 0
                assert exchange(port)[0] == 200
                print(f"{name} 会话超时和恢复通过，耗时 {elapsed:.2f} 秒", flush=True)


def idle_timeout(binary):
    with tempfile.TemporaryDirectory(prefix="ofm-idle-") as directory, upstream():
        path = Path(directory) / "settings.json"
        settings(path, {"managementKey": KEY})
        with contextlib.ExitStack() as stack:
            sockets = []
            for name, command in [("Node", REFERENCE), ("Rust", [str(Path(binary).resolve())])]:
                port, _ = stack.enter_context(running(command, path))
                conn = stack.enter_context(contextlib.closing(
                    http.client.HTTPConnection("127.0.0.1", port, timeout=72),
                ))
                conn.request("GET", "/healthz")
                response = conn.getresponse()
                assert response.getheader("Keep-Alive") == "timeout=65"
                assert response.read() == b"ok"
                sockets.append((name, conn.sock, time.monotonic()))

            def wait_for_close(item):
                name, sock, start = item
                assert sock.recv(1) == b""
                elapsed = time.monotonic() - start
                assert 65 <= elapsed <= 70, (name, elapsed)
                return f"{name} 空闲长连接在 {elapsed:.2f} 秒关闭"

            with ThreadPoolExecutor(max_workers=2) as pool:
                for message in pool.map(wait_for_close, sockets):
                    print(message, flush=True)


def pipeline_fixture(command, path):
    first = b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n"
    malformed = b"GET / HTTP/1.1\r\nHost: localhost\r\nInvalid header\r\nConnection: close\r\n\r\n"
    last = b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    body = b"GET / HTTP/1.1\r\nInvalid header\r\n\r\n"
    post = (b"POST /digest HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body)
    cases = {
        "合并发送的流水线异常头": (first + malformed, [400]),
        "分段发送的流水线异常头": ((first, malformed[:20], malformed[20:]), [400]),
        "合法流水线按序响应": (first + last, [200, 200]),
        "定长请求体内的 HTTP 字节仍是正文": (post + last, [200, 200]),
    }
    results = {}
    with upstream() as fixture, running(command, path) as (port, _):
        fixture.session_delay = 0.2
        for index, (name, (request, expected)) in enumerate(cases.items()):
            settings(path, {"managementKey": f"{KEY}-{index}"})
            messages, remaining = wire(port, request)
            lines = [status for status, _ in messages]
            lines += re.findall(rb"HTTP/1\.1 [0-9]{3} [^\r\n]+", remaining)
            statuses = [int(line.split()[1]) for line in lines]
            assert statuses == expected, (name, statuses, expected)
            if statuses == [400]:
                assert messages == [(b"HTTP/1.1 400 Bad Request", {b"connection": [b"close"]})]
                assert not remaining
            if request == post + last:
                assert remaining.startswith(hashlib.sha256(body).hexdigest().encode())
            results[name] = statuses
    return results


def pipeline(binary):
    with tempfile.TemporaryDirectory(prefix="ofm-pipeline-") as directory:
        path = Path(directory) / "settings.json"
        for name, command in [("Node", REFERENCE), ("Rust", [str(Path(binary).resolve())])]:
            print(f"{name}: {pipeline_fixture(command, path)}")


def replay(mode, binary):
    with tempfile.TemporaryDirectory(prefix="ofm-verify-") as directory, upstream():
        path = Path(directory) / "settings.json"
        settings(path, {"managementKey": KEY})
        env = app_env(path)
        command = ["rust-anything", "app"]
        if mode == "record":
            command += ["record", ".", "--http", "--no-traps", "--inputs",
                        "rust/app-cases/requests.jsonl", "--", *REFERENCE]
        else:
            command += ["replay", "."]
            if binary:
                port = str(free_port())
                env["OFM_PROXY_PORT"] = port
                command += ["--build", f'"{Path(binary).resolve()}"', "--port", port]
        subprocess.run(command, cwd=ROOT, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["record", "replay", "diff", "bench", "timeout", "idle",
                                         "pipeline"])
    parser.add_argument("--binary")
    args = parser.parse_args()
    checks = {"diff": diff, "bench": bench, "timeout": live_timeout, "idle": idle_timeout,
              "pipeline": pipeline}
    if args.mode in checks:
        if not args.binary:
            parser.error(f"--binary is required for {args.mode}")
        checks[args.mode](args.binary)
    else:
        replay(args.mode, args.binary)


if __name__ == "__main__":
    main()
