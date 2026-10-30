use lyph_collector_core::{
    CaptureAck, Config, DeviceApi, Engine, Event, RemoteConfig, Status, Store, SyncError,
};
use std::{
    collections::HashSet,
    sync::{Arc, Mutex},
};

const STAMP: &str = "2026-10-05T00:00:00+00:00";
const IMAGE: &[u8] = b"\x89PNG\r\n\x1a\nfixture-bytes";

#[derive(Clone, Copy)]
enum Behavior {
    Success,
    Offline,
    AcceptedWithoutAck,
    WrongAck,
    Revoked,
}
#[derive(Clone)]
struct Api {
    behavior: Arc<Mutex<Behavior>>,
    accepted: Arc<Mutex<HashSet<String>>>,
    calls: Arc<Mutex<u32>>,
}
impl Api {
    fn new(behavior: Behavior) -> Self {
        Self {
            behavior: Arc::new(Mutex::new(behavior)),
            accepted: Arc::new(Mutex::new(HashSet::new())),
            calls: Arc::new(Mutex::new(0)),
        }
    }
}
fn remote() -> RemoteConfig {
    RemoteConfig {
        device_id: "device-one".into(),
        paused: false,
        capture_interval_seconds: 300,
        cache_limit_bytes: 1024 * 1024 * 1024,
        upload_concurrency: 1,
        max_upload_bytes: 20 * 1024 * 1024,
    }
}
impl DeviceApi for Api {
    fn config(&self) -> Result<RemoteConfig, SyncError> {
        if matches!(*self.behavior.lock().unwrap(), Behavior::Revoked) {
            Err(SyncError::Revoked)
        } else {
            Ok(remote())
        }
    }
    fn heartbeat(&self, _status: &Status) -> Result<RemoteConfig, SyncError> {
        self.config()
    }
    fn upload(&self, event: &Event) -> Result<CaptureAck, SyncError> {
        *self.calls.lock().unwrap() += 1;
        match *self.behavior.lock().unwrap() {
            Behavior::Offline => Err(SyncError::Network),
            Behavior::Revoked => Err(SyncError::Revoked),
            Behavior::WrongAck => Ok(CaptureAck {
                id: "unrelated-id".into(),
                duplicate: false,
            }),
            Behavior::AcceptedWithoutAck => {
                self.accepted.lock().unwrap().insert(event.id.clone());
                Err(SyncError::Network)
            }
            Behavior::Success => {
                let duplicate = !self.accepted.lock().unwrap().insert(event.id.clone());
                Ok(CaptureAck {
                    id: event.id.clone(),
                    duplicate,
                })
            }
        }
    }
}
fn config() -> Config {
    Config::new(
        "https://site.example".into(),
        "unit-test-device-token".into(),
    )
    .unwrap()
}

#[test]
fn offline_restart_recovers_the_same_event_and_bytes() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path()).unwrap();
    let api = Api::new(Behavior::Offline);
    let event = store
        .enqueue("screenshot", "", Some(IMAGE), STAMP, 1024)
        .unwrap();
    let path = event.file_path.clone().unwrap();
    let mut engine = Engine::new(store.clone(), config(), api.clone()).unwrap();
    assert!(!engine.upload_one(100).unwrap());
    assert!(path.exists());
    assert_eq!(store.status().unwrap().pending_count, 1);
    assert!(store.event(&event.id).unwrap().unwrap().next_attempt_at > 100);
    drop(engine);
    drop(store);
    let reopened = Store::open(dir.path()).unwrap();
    assert_eq!(std::fs::read(&path).unwrap(), IMAGE);
    *api.behavior.lock().unwrap() = Behavior::Success;
    let mut engine = Engine::new(reopened.clone(), config(), api.clone()).unwrap();
    assert!(engine.upload_one(10000).unwrap());
    assert!(!path.exists());
    assert_eq!(reopened.status().unwrap().pending_count, 0);
    assert_eq!(reopened.status().unwrap().confirmed_count, 1);
    assert!(api.accepted.lock().unwrap().contains(&event.id));
}

#[test]
fn lost_confirmation_reuploads_uuid_and_is_not_a_second_event() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path()).unwrap();
    let api = Api::new(Behavior::AcceptedWithoutAck);
    let event = store
        .enqueue("photo", "", Some(IMAGE), STAMP, 1024)
        .unwrap();
    let mut engine = Engine::new(store.clone(), config(), api.clone()).unwrap();
    assert!(!engine.upload_one(10).unwrap());
    drop(engine);
    *api.behavior.lock().unwrap() = Behavior::Success;
    let mut engine = Engine::new(Store::open(dir.path()).unwrap(), config(), api.clone()).unwrap();
    assert!(engine.upload_one(10000).unwrap());
    assert_eq!(api.accepted.lock().unwrap().len(), 1);
    assert_eq!(*api.calls.lock().unwrap(), 2);
    assert!(store.event(&event.id).unwrap().is_none());
}

#[test]
fn same_content_has_distinct_event_ids_and_shared_object_lifetime() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path()).unwrap();
    let one = store
        .enqueue("screenshot", "", Some(IMAGE), STAMP, 1024)
        .unwrap();
    let two = store
        .enqueue(
            "screenshot",
            "",
            Some(IMAGE),
            "2026-10-05T00:05:00+00:00",
            1024,
        )
        .unwrap();
    assert_ne!(one.id, two.id);
    assert_eq!(one.sha256, two.sha256);
    assert_eq!(store.status().unwrap().cache_bytes, IMAGE.len() as u64);
    store.acknowledge(&one.id, &one.id).unwrap();
    assert!(two.file_path.as_ref().unwrap().exists());
    store.acknowledge(&two.id, &two.id).unwrap();
    assert!(!two.file_path.as_ref().unwrap().exists());
    assert_eq!(store.status().unwrap().confirmed_count, 2);
}

#[test]
fn full_cache_preserves_queued_event_and_refuses_new_data() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path()).unwrap();
    let one = store
        .enqueue("photo", "", Some(IMAGE), STAMP, IMAGE.len() as u64)
        .unwrap();
    assert!(
        store
            .enqueue("note", "new", None, STAMP, IMAGE.len() as u64)
            .is_err()
    );
    assert_eq!(store.status().unwrap().capture_status, "cache_full");
    assert!(one.file_path.unwrap().exists());
    assert_eq!(store.status().unwrap().pending_count, 1);
}

#[test]
fn wrong_ack_never_deletes_files_and_revoked_device_retains_queue() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path()).unwrap();
    let api = Api::new(Behavior::WrongAck);
    let event = store
        .enqueue("photo", "", Some(IMAGE), STAMP, 1024)
        .unwrap();
    let mut engine = Engine::new(store.clone(), config(), api.clone()).unwrap();
    assert!(!engine.upload_one(1).unwrap());
    assert_eq!(store.status().unwrap().confirmed_count, 0);
    assert!(event.file_path.unwrap().exists());
    *api.behavior.lock().unwrap() = Behavior::Revoked;
    engine.refresh_config().unwrap();
    assert!(store.status().unwrap().revoked);
    assert!(!engine.upload_one(1000).unwrap());
    assert_eq!(store.status().unwrap().pending_count, 1);
}

#[test]
fn only_one_sync_engine_can_hold_the_upload_slot() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path()).unwrap();
    let api = Api::new(Behavior::Success);
    let one = Engine::new(store.clone(), config(), api.clone()).unwrap();
    assert!(Engine::new(store.clone(), config(), api.clone()).is_err());
    drop(one);
    assert!(Engine::new(store, config(), api).is_ok());
}

#[test]
fn configuration_redacts_token_and_checks_origin() {
    let dir = tempfile::tempdir().unwrap();
    let value = config();
    value.save(dir.path()).unwrap();
    assert_eq!(
        Config::load(dir.path()).unwrap().device_token,
        value.device_token
    );
    assert!(!format!("{:?}", value).contains("unit-test-device-token"));
    assert!(
        Config::new(
            "http://public.example".into(),
            "unit-test-device-token".into()
        )
        .is_err()
    );
    assert!(
        Config::new(
            "http://127.0.0.1:8000".into(),
            "unit-test-device-token".into()
        )
        .is_ok()
    );
}

#[test]
fn gc_after_committed_ack_removes_orphaned_bytes() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path()).unwrap();
    let event = store
        .enqueue("photo", "", Some(IMAGE), STAMP, 1024)
        .unwrap();
    // Simulate crash after acknowledged event was committed but before file GC.
    store
        .connect()
        .unwrap()
        .execute("DELETE FROM outbox WHERE id=?", [&event.id])
        .unwrap();
    assert!(event.file_path.as_ref().unwrap().exists());
    Store::open(dir.path()).unwrap();
    assert!(!event.file_path.unwrap().exists());
}
