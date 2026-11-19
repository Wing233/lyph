use std::error::Error;
use std::future::{Future, poll_fn, ready};
use std::io;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::task::{Context, Poll, ready as poll_ready};
use std::time::Duration;

use bytes::BytesMut;
use futures_util::{TryStreamExt, stream};
use http_body_util::{BodyExt, Full, StreamBody, combinators::UnsyncBoxBody};
use hyper::body::{Body as HttpBody, Bytes, Frame, Incoming};
use hyper::client::conn::http1;
use hyper::header::{self, HeaderMap, HeaderValue};
use hyper::rt::{Sleep, Timer};
use hyper::service::service_fn;
use hyper::{Method, Request, Response, StatusCode, Version};
use hyper_util::rt::{TokioIo, TokioTimer};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, DuplexStream, ReadBuf};
use tokio::net::TcpStream;
use tokio::net::tcp::{OwnedReadHalf, OwnedWriteHalf};
use tokio::sync::watch;
use tokio::time::{Instant, sleep_until};

use crate::{AUTHORITY, Proxy, UPSTREAM, log};

const MAX_BODY: usize = 32 * 1024 * 1024;
const TIMEOUT: Duration = Duration::from_secs(600);
const HEADER_TIMEOUT: Duration = Duration::from_secs(60);
const KEEP_ALIVE_TIMEOUT: Duration = Duration::from_secs(66);
const BAD_REQUEST: &[u8] = b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n";
const HEAD_TOO_LARGE: &[u8] =
    b"HTTP/1.1 431 Request Header Fields Too Large\r\nConnection: close\r\n\r\n";
const MAX_HEAD: usize = 16 * 1024;
const UNAVAILABLE: &str = "模型工作台暂时不可用";
type Body = UnsyncBoxBody<Bytes, hyper::Error>;

const REQUEST_EXCLUDED: &[&str] = &[
    "host",
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "upgrade",
    "te",
    "trailer",
    "cookie",
    "authorization",
    "origin",
    "referer",
    "content-length",
];
const RESPONSE_EXCLUDED: &[&str] = &[
    "connection",
    "keep-alive",
    "transfer-encoding",
    "set-cookie",
];
const SINGLE_VALUE: &[&str] = &[
    "age",
    "authorization",
    "content-length",
    "content-type",
    "etag",
    "expires",
    "from",
    "host",
    "if-modified-since",
    "if-unmodified-since",
    "last-modified",
    "location",
    "max-forwards",
    "proxy-authorization",
    "referer",
    "retry-after",
    "server",
    "user-agent",
];

const METHODS: &[&str] = &[
    "ACL",
    "BIND",
    "CHECKOUT",
    "CONNECT",
    "COPY",
    "DELETE",
    "GET",
    "HEAD",
    "LINK",
    "LOCK",
    "M-SEARCH",
    "MERGE",
    "MKACTIVITY",
    "MKCALENDAR",
    "MKCOL",
    "MOVE",
    "NOTIFY",
    "OPTIONS",
    "PATCH",
    "POST",
    "PROPFIND",
    "PROPPATCH",
    "PURGE",
    "PUT",
    "QUERY",
    "REBIND",
    "REPORT",
    "SEARCH",
    "SOURCE",
    "SUBSCRIBE",
    "TRACE",
    "UNBIND",
    "UNLINK",
    "UNLOCK",
    "UNSUBSCRIBE",
];

fn full(bytes: Bytes) -> Body {
    Full::new(bytes)
        .map_err(|never| match never {})
        .boxed_unsync()
}

fn text(bytes: &'static str) -> Body {
    StreamBody::new(stream::once(ready(Ok(Frame::data(Bytes::from_static(
        bytes.as_bytes(),
    ))))))
    .boxed_unsync()
}

fn response(status: StatusCode, body: Body) -> Response<Body> {
    let mut response = Response::new(body);
    *response.status_mut() = status;
    response
}

fn local(status: StatusCode, content_type: &'static str, body: Body) -> Response<Body> {
    let mut response = response(status, body);
    response
        .headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

fn json(status: StatusCode, message: &str) -> Response<Body> {
    let body = serde_json::json!({"error": message}).to_string();
    let length = body.len();
    let mut response = local(
        status,
        "application/json; charset=utf-8",
        full(Bytes::from(body)),
    );
    response
        .headers_mut()
        .insert(header::CONTENT_LENGTH, HeaderValue::from(length));
    response
}

fn has_token(headers: &HeaderMap, name: header::HeaderName, token: &str) -> bool {
    headers.get_all(name).iter().any(|value| {
        value
            .as_bytes()
            .split(|&byte| byte == b',')
            .any(|item| item.trim_ascii().eq_ignore_ascii_case(token.as_bytes()))
    })
}

pub async fn serve(proxy: &Proxy, allowed: bool, socket: TcpStream) {
    let (reader, writer) = socket.into_split();
    let (input, feed) = tokio::io::duplex(MAX_HEAD);
    let (socket, state) = ClientIo::new(input, writer);
    let service = service_fn(|request| Box::pin(handle(proxy, allowed, &state, request)));
    let mut connection = hyper::server::conn::http1::Builder::new()
        .timer(HeaderTimer(Arc::clone(&state)))
        .header_read_timeout(HEADER_TIMEOUT)
        .max_headers(2000)
        .max_header_size(MAX_HEAD)
        .writev(false)
        .serve_connection(TokioIo::new(socket), service);
    let reply = tokio::select! {
        biased;
        result = feed_client(reader, feed) => {
            result.ok().filter(|_| !state.response_started.load(Ordering::Relaxed))
        },
        result = poll_fn(|cx| connection.poll_without_shutdown(cx)) => {
            result.err().and_then(|error| {
                let rejected = state.rejected_method.load(Ordering::Relaxed);
                let parser_error = state.replace_parse_error.load(Ordering::Relaxed)
                    && error.is_parse();
                (rejected || parser_error).then(|| {
                    if error.is_parse_too_large() { HEAD_TOO_LARGE } else { BAD_REQUEST }
                })
            })
        }
    };
    if let Some(reply) = reply {
        let mut socket = connection.into_parts().io.into_inner().socket;
        let _ = socket.write_all(reply).await;
    }
}

// Node can reject a later request while the first response is still pending.
// Keep reading ahead independently of Hyper, which runs its service sequentially.
async fn feed_client(
    mut reader: OwnedReadHalf,
    mut feed: DuplexStream,
) -> io::Result<&'static [u8]> {
    let mut scan = PipelineScan::default();
    let mut bytes = [0; 8192];
    loop {
        let read = reader.read(&mut bytes).await?;
        if read == 0 {
            feed.shutdown().await?;
            return std::future::pending().await;
        }
        if let Some(error) = scan.inspect(&bytes[..read]) {
            return Ok(error);
        }
        feed.write_all(&bytes[..read]).await?;
    }
}

#[derive(Default)]
struct PipelineScan {
    head: BytesMut,
    body_left: usize,
    seen: bool,
    disabled: bool,
}

impl PipelineScan {
    fn inspect(&mut self, mut bytes: &[u8]) -> Option<&'static [u8]> {
        if self.disabled {
            return None;
        }
        while !bytes.is_empty() {
            let skip = self.body_left.min(bytes.len());
            self.body_left -= skip;
            bytes = &bytes[skip..];
            if bytes.is_empty() {
                break;
            }
            // Only retain one head and at most one socket read beyond its end.
            let buffered = self.head.len();
            self.head.extend_from_slice(bytes);
            let mut slots = [httparse::EMPTY_HEADER; 128];
            let mut request = httparse::Request::new(&mut slots);
            let error = match request.parse(&self.head) {
                Ok(httparse::Status::Partial) if self.head.len() < MAX_HEAD => return None,
                Ok(httparse::Status::Complete(end)) if end <= MAX_HEAD => {
                    if self.seen
                        && request
                            .method
                            .is_some_and(|method| !METHODS.contains(&method))
                    {
                        return Some(BAD_REQUEST);
                    }
                    let Some(length) = Self::next_body(&request) else {
                        break;
                    };
                    self.body_left = length;
                    self.seen = true;
                    self.head.clear();
                    bytes = &bytes[end - buffered..];
                    continue;
                }
                Ok(_) => Some(HEAD_TOO_LARGE),
                // Hyper already handles header counts and request-line deviations.
                Err(httparse::Error::HeaderName | httparse::Error::HeaderValue) => {
                    Some(BAD_REQUEST)
                }
                Err(_) => None,
            };
            if self.seen && error.is_some() {
                return error;
            }
            break;
        }
        if !bytes.is_empty() {
            self.disabled = true;
            self.head = BytesMut::new();
        }
        None
    }

    fn next_body(request: &httparse::Request<'_, '_>) -> Option<usize> {
        if request.version != Some(1) || request.method == Some("CONNECT") {
            return None;
        }
        let mut length = None;
        for field in &*request.headers {
            let name = field.name;
            let value = field.value.trim_ascii();
            if name.eq_ignore_ascii_case("transfer-encoding")
                || name.eq_ignore_ascii_case("upgrade")
                || (name.eq_ignore_ascii_case("connection")
                    && value
                        .split(|&b| b == b',')
                        .any(|part| part.trim_ascii().eq_ignore_ascii_case(b"close")))
            {
                // Leave uncertain message boundaries and upgrades to Hyper.
                return None;
            }
            if name.eq_ignore_ascii_case("content-length") {
                if length.is_some() || !value.iter().all(u8::is_ascii_digit) {
                    return None;
                }
                length = Some(std::str::from_utf8(value).ok()?.parse().ok()?);
            }
        }
        Some(length.unwrap_or(0))
    }
}

async fn handle(
    proxy: &Proxy,
    allowed: bool,
    state: &Arc<ClientState>,
    request: Request<Incoming>,
) -> io::Result<Response<ClientBody>> {
    state.replace_parse_error.store(false, Ordering::Relaxed);
    if !METHODS.contains(&request.method().as_str()) {
        state.rejected_method.store(true, Ordering::Relaxed);
        return Err(io::Error::other("unrecognized HTTP method"));
    }
    state.received.store(true, Ordering::Relaxed);
    let is_head = request.method() == Method::HEAD;
    let keep_alive = request.version() == Version::HTTP_11
        && !has_token(request.headers(), header::CONNECTION, "close");
    let mut response = route(proxy, allowed, &state.interim, request).await?;
    if keep_alive && !response.headers().contains_key(header::CONNECTION) {
        response
            .headers_mut()
            .insert(header::CONNECTION, HeaderValue::from_static("keep-alive"));
        response
            .headers_mut()
            .insert("keep-alive", HeaderValue::from_static("timeout=65"));
    } else {
        response
            .headers_mut()
            .insert(header::CONNECTION, HeaderValue::from_static("close"));
    }
    state.output_unflushed.store(true, Ordering::Relaxed);
    state.response_complete.store(
        is_head
            || matches!(
                response.status(),
                StatusCode::NO_CONTENT | StatusCode::NOT_MODIFIED
            )
            || response
                .headers()
                .get(header::CONTENT_LENGTH)
                .is_some_and(|v| v == "0")
            || response.body().is_end_stream(),
        Ordering::Relaxed,
    );
    Ok(response.map(|body| ClientBody {
        body,
        state: Arc::clone(state),
    }))
}

async fn route(
    proxy: &Proxy,
    allowed: bool,
    interim: &AtomicBool,
    request: Request<Incoming>,
) -> io::Result<Response<Body>> {
    let (parts, mut body) = request.into_parts();
    if parts.method == Method::CONNECT
        || (parts.headers.contains_key(header::UPGRADE)
            && has_token(&parts.headers, header::CONNECTION, "upgrade"))
    {
        return Err(io::Error::other("HTTP upgrade is not handled"));
    }
    let target = parts.uri.to_string();
    let health = target.split('?').next() == Some("/healthz");
    if parts.version == Version::HTTP_11 {
        if !parts.headers.contains_key(header::HOST) {
            let mut answer = response(StatusCode::BAD_REQUEST, text(""));
            answer
                .headers_mut()
                .insert(header::CONNECTION, HeaderValue::from_static("close"));
            return Ok(answer);
        }
        if parts.headers.contains_key(header::EXPECT) {
            let values = parts.headers.get_all(header::EXPECT);
            if !values
                .iter()
                .any(|value| expects_continue(value.as_bytes()))
            {
                return Ok(response(StatusCode::EXPECTATION_FAILED, text("")));
            }
            // Hyper emits 100 when an exact Expect header is followed by a body we poll.
            let hyper_emits = values
                .iter()
                .any(|value| value.as_bytes().eq_ignore_ascii_case(b"100-continue"))
                && !body.is_end_stream()
                && allowed
                && !health;
            if !hyper_emits {
                interim.store(true, Ordering::Relaxed);
            }
        }
    }
    if !allowed {
        return Ok(json(StatusCode::FORBIDDEN, "forbidden"));
    }
    if health {
        let (status, message) = match proxy.sessions.ensure(false).await {
            Ok(_) => (StatusCode::OK, "ok"),
            Err(_) => (StatusCode::SERVICE_UNAVAILABLE, "unavailable"),
        };
        return Ok(local(status, "text/plain; charset=utf-8", text(message)));
    }

    let mut buffer = BytesMut::new();
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(io::Error::other)?;
        if let Ok(data) = frame.into_data() {
            if data.len() > MAX_BODY - buffer.len() {
                // Node destroys the request socket when the limit is crossed.
                return Err(io::Error::other("request body too large"));
            }
            buffer.extend_from_slice(&data);
        }
    }
    let body = buffer.freeze();

    for force in [false, true] {
        let cookie = match proxy.sessions.ensure(force).await {
            Ok(cookie) => cookie,
            Err(error) => {
                log(format_args!("OFM 管理会话创建失败: {error}"));
                return Ok(json(StatusCode::SERVICE_UNAVAILABLE, UNAVAILABLE));
            }
        };
        let headers = upstream_headers(&parts.headers, &parts.method, cookie, body.len())?;
        let mut request = Request::new(Full::new(body.clone()));
        *request.method_mut() = parts.method.clone();
        *request.uri_mut() = parts.uri.clone();
        *request.headers_mut() = headers;
        let upstream = match send(request, TIMEOUT).await {
            Ok(upstream) => upstream,
            Err(error) => {
                let reason = error.message("OFM upstream timed out");
                log(format_args!("OFM 上游请求失败: {reason}"));
                if matches!(
                    reason.as_str(),
                    "ECONNREFUSED" | "EHOSTUNREACH" | "ENETUNREACH"
                ) {
                    proxy.schedule_exit(&reason);
                }
                return Ok(json(StatusCode::BAD_GATEWAY, UNAVAILABLE));
            }
        };
        let (parts, body) = upstream.into_parts();
        if parts.status == StatusCode::UNAUTHORIZED && !force {
            if drain(body).await.is_err() {
                // The reference waits for the 401 response's end event before retrying.
                std::future::pending::<()>().await;
            }
            proxy.sessions.invalidate().await;
            log(format_args!("OFM 管理会话已失效，重新登录"));
            continue;
        }
        // The original streams payload bytes and does not send upstream trailer fields.
        let body = StreamBody::new(body.into_data_stream().map_ok(Frame::data)).boxed_unsync();
        let mut answer = response(parts.status, body);
        *answer.headers_mut() = joined(&parts.headers, RESPONSE_EXCLUDED)?;
        return Ok(answer);
    }
    Ok(json(StatusCode::INTERNAL_SERVER_ERROR, UNAVAILABLE))
}

fn expects_continue(value: &[u8]) -> bool {
    const TOKEN: &[u8] = b"100-continue";
    let word = |byte: &u8| byte.is_ascii_alphanumeric() || *byte == b'_';
    value.windows(TOKEN.len()).enumerate().any(|(at, token)| {
        token.eq_ignore_ascii_case(TOKEN)
            && (at == 0 || !word(&value[at - 1]))
            && value.get(at + TOKEN.len()).is_none_or(|byte| !word(byte))
    })
}

// Node writes informational replies even when the handler never reads a request body,
// and always writes an HTTP/1.1 status line. Hyper otherwise manages the message framing.
struct ClientIo {
    input: DuplexStream,
    socket: OwnedWriteHalf,
    state: Arc<ClientState>,
    interim_written: usize,
    first_write: bool,
    version_written: usize,
}

#[derive(Default)]
struct ClientState {
    interim: AtomicBool,
    received: AtomicBool,
    rejected_method: AtomicBool,
    output_unflushed: AtomicBool,
    replace_parse_error: AtomicBool,
    response_started: AtomicBool,
    response_complete: AtomicBool,
    reads: AtomicU64,
}

struct ClientBody {
    body: Body,
    state: Arc<ClientState>,
}

impl HttpBody for ClientBody {
    type Data = Bytes;
    type Error = hyper::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
        let frame = Pin::new(&mut self.body).poll_frame(cx);
        if frame.is_ready() {
            // End-of-stream may also enqueue the terminal chunk.
            self.state.output_unflushed.store(true, Ordering::Relaxed);
            if matches!(frame, Poll::Ready(None)) || self.body.is_end_stream() {
                self.state.response_complete.store(true, Ordering::Relaxed);
            }
        }
        frame
    }

    fn is_end_stream(&self) -> bool {
        self.body.is_end_stream()
    }

    fn size_hint(&self) -> hyper::body::SizeHint {
        self.body.size_hint()
    }
}

// Node applies its keep-alive timeout plus a one-second buffer between messages.
// Reading the next header switches the connection back to the header timeout.
struct HeaderTimer(Arc<ClientState>);

impl Timer for HeaderTimer {
    fn now(&self) -> std::time::Instant {
        Instant::now().into_std()
    }

    fn sleep(&self, duration: Duration) -> Pin<Box<dyn Sleep>> {
        self.sleep_until(self.now() + duration)
    }

    fn sleep_until(&self, deadline: std::time::Instant) -> Pin<Box<dyn Sleep>> {
        // Hyper starts this timer before parsing each head. Only take over its
        // error response once all bytes from the previous response have flushed.
        self.0.replace_parse_error.store(
            !self.0.output_unflushed.load(Ordering::Relaxed),
            Ordering::Relaxed,
        );
        if !self.0.received.load(Ordering::Relaxed) {
            return TokioTimer::new().sleep_until(deadline);
        }
        Box::pin(KeepAliveSleep {
            reads: Some(self.0.reads.load(Ordering::Relaxed)),
            state: Arc::clone(&self.0),
            sleep: Box::pin(sleep_until(
                (deadline + KEEP_ALIVE_TIMEOUT - HEADER_TIMEOUT).into(),
            )),
        })
    }
}

struct KeepAliveSleep {
    reads: Option<u64>,
    state: Arc<ClientState>,
    sleep: Pin<Box<tokio::time::Sleep>>,
}

impl Future for KeepAliveSleep {
    type Output = ();

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        if self
            .reads
            .is_some_and(|reads| reads != self.state.reads.load(Ordering::Relaxed))
        {
            self.reads = None;
            self.sleep.as_mut().reset(Instant::now() + HEADER_TIMEOUT);
        }
        self.sleep.as_mut().poll(cx)
    }
}

impl Sleep for KeepAliveSleep {}

impl ClientIo {
    fn new(input: DuplexStream, socket: OwnedWriteHalf) -> (Self, Arc<ClientState>) {
        let state = Arc::new(ClientState::default());
        let io = Self {
            input,
            socket,
            state: Arc::clone(&state),
            interim_written: 0,
            first_write: true,
            version_written: 0,
        };
        (io, state)
    }

    fn poll_interim(&mut self, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        const MESSAGE: &[u8] = b"HTTP/1.1 100 Continue\r\n\r\n";
        if self.state.interim.load(Ordering::Relaxed) {
            while self.interim_written < MESSAGE.len() {
                let n = poll_ready!(
                    Pin::new(&mut self.socket).poll_write(cx, &MESSAGE[self.interim_written..])
                )?;
                if n == 0 {
                    return Poll::Ready(Err(io::ErrorKind::WriteZero.into()));
                }
                self.interim_written += n;
            }
            self.interim_written = 0;
            self.state.interim.store(false, Ordering::Relaxed);
        }
        Poll::Ready(Ok(()))
    }
}

impl AsyncRead for ClientIo {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let before = buffer.filled().len();
        let result = Pin::new(&mut self.input).poll_read(cx, buffer);
        if buffer.filled().len() > before {
            self.state.reads.fetch_add(1, Ordering::Relaxed);
        }
        result
    }
}

impl AsyncWrite for ClientIo {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<io::Result<usize>> {
        // The parser is active, the prior response has flushed, and no new
        // service response exists yet. Let serve() write the bare parser error.
        if self.state.replace_parse_error.load(Ordering::Relaxed) {
            return Poll::Ready(Ok(buffer.len()));
        }
        poll_ready!(self.poll_interim(cx))?;
        if self.first_write && (self.version_written > 0 || buffer.starts_with(b"HTTP/1.0 ")) {
            // HTTP/1.0 connections close after this response. Consume the version bytes
            // from Hyper's buffer as the replacement bytes are successfully written.
            let version = &b"HTTP/1.1 "[self.version_written..];
            let n = poll_ready!(Pin::new(&mut self.socket).poll_write(cx, version))?;
            if n > 0 {
                self.state.response_started.store(true, Ordering::Relaxed);
            }
            self.version_written += n;
            self.first_write = self.version_written < b"HTTP/1.1 ".len();
            return Poll::Ready(Ok(n));
        }
        let n = poll_ready!(Pin::new(&mut self.socket).poll_write(cx, buffer))?;
        if n > 0 {
            self.state.response_started.store(true, Ordering::Relaxed);
        }
        self.first_write &= n == 0;
        Poll::Ready(Ok(n))
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        poll_ready!(self.poll_interim(cx))?;
        poll_ready!(Pin::new(&mut self.socket).poll_flush(cx))?;
        self.state.output_unflushed.store(false, Ordering::Relaxed);
        if self.state.response_complete.load(Ordering::Relaxed) {
            self.state.response_started.store(false, Ordering::Relaxed);
        }
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.socket).poll_shutdown(cx)
    }
}

fn joined(headers: &HeaderMap, excluded: &[&str]) -> io::Result<HeaderMap> {
    let mut result = HeaderMap::new();
    for name in headers
        .keys()
        .filter(|name| !excluded.contains(&name.as_str()))
    {
        let all = headers.get_all(name);
        let mut values = all.iter();
        let Some(first) = values.next() else { continue };
        if SINGLE_VALUE.contains(&name.as_str()) {
            result.insert(name, first.clone());
        } else if name == header::SET_COOKIE {
            for value in all {
                result.append(name, value.clone());
            }
        } else if let Some(second) = values.next() {
            let separator = if name == header::COOKIE { b"; " } else { b", " };
            let mut bytes = first.as_bytes().to_vec();
            for value in std::iter::once(second).chain(values) {
                bytes.extend_from_slice(separator);
                bytes.extend_from_slice(value.as_bytes());
            }
            result.insert(
                name,
                HeaderValue::from_bytes(&bytes).map_err(io::Error::other)?,
            );
        } else {
            result.insert(name, first.clone());
        }
    }
    Ok(result)
}

fn upstream_headers(
    headers: &HeaderMap,
    method: &Method,
    cookie: HeaderValue,
    length: usize,
) -> io::Result<HeaderMap> {
    let mut answer = joined(headers, REQUEST_EXCLUDED)?;
    answer.insert(header::HOST, HeaderValue::from_static(AUTHORITY));
    answer.insert(header::COOKIE, cookie);
    if headers
        .get_all(header::ORIGIN)
        .iter()
        .any(|v| !v.is_empty())
    {
        answer.insert(
            header::ORIGIN,
            HeaderValue::from_static("http://127.0.0.1:18900"),
        );
    }
    if headers.get(header::REFERER).is_some_and(|v| !v.is_empty()) {
        answer.insert(
            header::REFERER,
            HeaderValue::from_static("http://127.0.0.1:18900/"),
        );
    }
    if length > 0 || !matches!(*method, Method::GET | Method::HEAD) {
        answer.insert(header::CONTENT_LENGTH, HeaderValue::from(length));
    }
    answer.insert(header::CONNECTION, HeaderValue::from_static("close"));
    Ok(answer)
}

pub async fn drain(mut body: Incoming) -> Result<(), hyper::Error> {
    while let Some(frame) = body.frame().await {
        frame?;
    }
    Ok(())
}

pub enum UpstreamError {
    Io(io::Error),
    Http(hyper::Error),
    Idle,
}

impl UpstreamError {
    pub fn message(&self, idle: &str) -> String {
        let error: &(dyn Error + 'static) = match self {
            Self::Io(error) => error,
            Self::Http(error) if error.is_incomplete_message() => return "ECONNRESET".into(),
            Self::Http(error) => error,
            Self::Idle => return idle.into(),
        };
        let mut source = Some(error);
        while let Some(error) = source {
            if let Some(error) = error.downcast_ref::<io::Error>() {
                match error.kind() {
                    io::ErrorKind::ConnectionRefused => return "ECONNREFUSED".into(),
                    io::ErrorKind::HostUnreachable => return "EHOSTUNREACH".into(),
                    io::ErrorKind::NetworkUnreachable => return "ENETUNREACH".into(),
                    io::ErrorKind::ConnectionReset => return "ECONNRESET".into(),
                    io::ErrorKind::BrokenPipe => return "EPIPE".into(),
                    _ => {}
                }
            }
            source = error.source();
        }
        error.to_string()
    }
}

// Track actual socket progress for Node's inactivity timers, including mid-stream stalls.
struct ActivityIo {
    stream: TcpStream,
    activity: watch::Sender<Instant>,
}

impl AsyncRead for ActivityIo {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let before = buffer.filled().len();
        let result = Pin::new(&mut self.stream).poll_read(cx, buffer);
        if buffer.filled().len() > before {
            self.activity.send_replace(Instant::now());
        }
        result
    }
}

impl AsyncWrite for ActivityIo {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<io::Result<usize>> {
        let result = Pin::new(&mut self.stream).poll_write(cx, buffer);
        if matches!(result, Poll::Ready(Ok(size)) if size > 0) {
            self.activity.send_replace(Instant::now());
        }
        result
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_shutdown(cx)
    }
}

async fn inactive(activity: watch::Receiver<Instant>, timeout: Duration) {
    loop {
        let deadline = *activity.borrow() + timeout;
        sleep_until(deadline).await;
        if Instant::now() >= *activity.borrow() + timeout {
            return;
        }
    }
}

pub async fn send(
    request: Request<Full<Bytes>>,
    timeout: Duration,
) -> Result<Response<Incoming>, UpstreamError> {
    let socket = TcpStream::connect(UPSTREAM)
        .await
        .map_err(UpstreamError::Io)?;
    let _ = socket.set_nodelay(true);
    let (activity, clock) = watch::channel(Instant::now());
    let io = TokioIo::new(ActivityIo {
        stream: socket,
        activity,
    });
    let (mut sender, connection) = http1::Builder::new()
        .max_headers(2000)
        .max_header_size(16 * 1024)
        .handshake(io)
        .await
        .map_err(UpstreamError::Http)?;
    let expired = Arc::new(AtomicBool::new(false));
    let mark = Arc::clone(&expired);
    tokio::spawn(async move {
        tokio::select! {
            _ = connection => {}
            _ = inactive(clock, timeout) => mark.store(true, Ordering::Relaxed),
        }
    });
    sender.send_request(request).await.map_err(|error| {
        if expired.load(Ordering::Relaxed) {
            UpstreamError::Idle
        } else {
            UpstreamError::Http(error)
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expect_matches_node_ascii_word_boundaries() {
        assert!(expects_continue(b"other, 100-CoNtiNuE"));
        assert!(!expects_continue(b"prefix100-continue"));
        assert!(!expects_continue(b"100-continue_extra"));
    }

    #[tokio::test(start_paused = true)]
    async fn upstream_timeout_is_based_on_socket_inactivity() {
        let (activity, clock) = watch::channel(Instant::now());
        let watchdog = tokio::spawn(inactive(clock, Duration::from_secs(15)));
        tokio::time::advance(Duration::from_secs(14)).await;
        activity.send_replace(Instant::now());
        tokio::time::advance(Duration::from_secs(2)).await;
        assert!(!watchdog.is_finished());
        tokio::time::advance(Duration::from_secs(13)).await;
        watchdog.await.expect("inactivity watchdog finished");
    }

    #[tokio::test(start_paused = true)]
    async fn idle_timeout_switches_to_header_timeout_after_first_byte() {
        let state = Arc::new(ClientState::default());
        state.received.store(true, Ordering::Relaxed);
        let timer = HeaderTimer(Arc::clone(&state));
        let idle = tokio::spawn(timer.sleep(HEADER_TIMEOUT));
        tokio::time::advance(Duration::from_secs(65)).await;
        assert!(!idle.is_finished());
        tokio::time::advance(Duration::from_secs(1)).await;
        idle.await.expect("keep-alive timer finished");

        let header = tokio::spawn(timer.sleep(HEADER_TIMEOUT));
        tokio::time::advance(Duration::from_secs(30)).await;
        state.reads.fetch_add(1, Ordering::Relaxed);
        tokio::time::advance(Duration::from_secs(36)).await;
        assert!(!header.is_finished());
        header.await.expect("header timer finished");
    }
}
