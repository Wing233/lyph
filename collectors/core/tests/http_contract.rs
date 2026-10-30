use lyph_collector_core::{Config, DeviceApi, Store, api::HttpApi};
use std::{
    io::{Read, Write},
    net::TcpListener,
    sync::{Arc, Mutex},
    thread,
};

#[test]
fn real_multipart_uses_device_bearer_and_stable_capture_contract() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let captured = Arc::new(Mutex::new(String::new()));
    let copy = captured.clone();
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path()).unwrap();
    let event = store
        .enqueue(
            "note",
            "实际HTTP合同",
            None,
            "2026-10-05T00:00:00+00:00",
            1024,
        )
        .unwrap();
    let capture_id = event.id.clone();
    let server = thread::spawn(move || {
        let (mut socket, _) = listener.accept().unwrap();
        socket
            .set_read_timeout(Some(std::time::Duration::from_secs(5)))
            .unwrap();
        let mut request = Vec::new();
        let mut buffer = [0u8; 4096];
        loop {
            let n = socket.read(&mut buffer).unwrap();
            assert!(n > 0);
            request.extend_from_slice(&buffer[..n]);
            if let Some(start) = request.windows(4).position(|v| v == b"\r\n\r\n") {
                let header = String::from_utf8_lossy(&request[..start]).to_lowercase();
                let size: usize = header
                    .lines()
                    .find_map(|v| v.strip_prefix("content-length: "))
                    .unwrap()
                    .parse()
                    .unwrap();
                if request.len() >= start + 4 + size {
                    break;
                }
            }
        }
        *copy.lock().unwrap() = String::from_utf8(request).unwrap();
        let body = format!("{{\"id\":\"{}\",\"duplicate\":false}}", capture_id);
        write!(socket,"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body).unwrap();
    });
    let config = Config::new(
        format!("http://{}", address),
        "contract-test-device-token".into(),
    )
    .unwrap();
    let api = HttpApi::new(config).unwrap();
    let ack = api.upload(&event).unwrap();
    assert_eq!(ack.id, event.id);
    server.join().unwrap();
    let request = captured.lock().unwrap();
    assert!(request.starts_with("POST /api/v1/device/captures HTTP/1.1"));
    assert!(
        request
            .to_lowercase()
            .contains("authorization: bearer contract-test-device-token")
    );
    assert!(request.contains("name=\"capture_id\""));
    assert!(request.contains(&event.id));
    assert!(request.contains("name=\"captured_at\""));
    assert!(request.contains("name=\"source_kind\""));
    assert!(request.contains("实际HTTP合同"));
}
