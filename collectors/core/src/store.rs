use crate::config::{private_directory, private_file};

pub fn sha256(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
use anyhow::{Context, Result, bail};
use chrono::Utc;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    time::Duration,
};
use uuid::Uuid;

#[derive(Clone)]
pub struct Store {
    pub root: PathBuf,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub id: String,
    pub captured_at: String,
    pub source_kind: String,
    pub text: String,
    pub sha256: Option<String>,
    pub file_path: Option<PathBuf>,
    pub size: u64,
    pub attempts: u32,
    pub next_attempt_at: i64,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Status {
    pub pending_count: u64,
    pub cache_bytes: u64,
    pub confirmed_count: u64,
    pub local_paused: bool,
    pub remote_paused: bool,
    pub revoked: bool,
    pub capture_status: String,
    pub last_capture_at: Option<String>,
    pub last_error: Option<String>,
    pub capture_interval_seconds: u64,
    pub cache_limit_bytes: u64,
}

fn event_row(row: &rusqlite::Row<'_>, root: &Path) -> rusqlite::Result<Event> {
    let sha: Option<String> = row.get("sha256")?;
    Ok(Event {
        id: row.get("id")?,
        captured_at: row.get("captured_at")?,
        source_kind: row.get("source_kind")?,
        text: row.get("text")?,
        file_path: sha.as_ref().map(|s| root.join("objects").join(s)),
        sha256: sha,
        size: row.get::<_, i64>("size")? as u64,
        attempts: row.get("attempts")?,
        next_attempt_at: row.get("next_attempt_at")?,
    })
}

impl Store {
    pub fn open(root: impl AsRef<Path>) -> Result<Self> {
        let root = root.as_ref().to_path_buf();
        private_directory(&root)?;
        private_directory(&root.join("objects"))?;
        let value = Self { root };
        let conn = value.connect()?;
        conn.execute_batch("CREATE TABLE IF NOT EXISTS objects(sha256 TEXT PRIMARY KEY,size INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,captured_at TEXT NOT NULL,source_kind TEXT NOT NULL,
          text TEXT NOT NULL,sha256 TEXT REFERENCES objects(sha256),size INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at INTEGER NOT NULL DEFAULT 0,last_error TEXT);
          CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,confirmed_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS outbox_due ON outbox(next_attempt_at,captured_at);")?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                value.root.join("queue.sqlite3"),
                fs::Permissions::from_mode(0o600),
            )?;
        }
        // SQLite transactions serialize garbage collection against enqueue's durable file write.
        value.collect_garbage()?;
        Ok(value)
    }

    pub fn connect(&self) -> Result<Connection> {
        let conn = Connection::open(self.root.join("queue.sqlite3"))?;
        conn.busy_timeout(Duration::from_secs(5))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", true)?;
        Ok(conn)
    }

    pub fn enqueue(
        &self,
        source_kind: &str,
        text: &str,
        data: Option<&[u8]>,
        captured_at: &str,
        limit: u64,
    ) -> Result<Event> {
        if !matches!(source_kind, "screenshot" | "note" | "photo") || text.chars().count() > 16000 {
            bail!("采集类型或文本长度无效");
        }
        chrono::DateTime::parse_from_rfc3339(captured_at).context("采集时间必须含时区")?;
        if source_kind == "note" && text.trim().is_empty()
            || source_kind != "note" && data.is_none()
        {
            bail!("速记需要文本，图片需要文件");
        }
        if data.is_some_and(|v| v.len() > 20 * 1024 * 1024) {
            bail!("图片超过20MiB");
        }
        let sha = data.map(sha256);
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let image_bytes: u64 =
            tx.query_row("SELECT coalesce(sum(size),0) FROM objects", [], |r| {
                r.get::<_, i64>(0)
            })? as u64;
        let note_bytes: u64 = tx.query_row(
            "SELECT coalesce(sum(length(CAST(text AS BLOB))),0) FROM outbox",
            [],
            |r| r.get::<_, i64>(0),
        )? as u64;
        let existing = if let Some(sha) = &sha {
            tx.query_row("SELECT size FROM objects WHERE sha256=?", [sha], |r| {
                r.get::<_, i64>(0)
            })
            .optional()?
        } else {
            None
        };
        let added = if existing.is_some() {
            0
        } else {
            data.map_or(0, |v| v.len() as u64)
        } + text.len() as u64;
        if image_bytes + note_bytes + added > limit {
            tx.execute(
                "INSERT OR REPLACE INTO state VALUES('last_error','cache_full')",
                [],
            )?;
            tx.execute(
                "INSERT OR REPLACE INTO state VALUES('capture_status','cache_full')",
                [],
            )?;
            tx.commit()?;
            bail!("cache_full：缓存达到上限，已暂停新增采集；已有队列仍保留");
        }
        if let (Some(bytes), Some(sha)) = (data, &sha) {
            if existing.is_none() {
                let final_path = self.root.join("objects").join(sha);
                let temporary = self
                    .root
                    .join("objects")
                    .join(format!("{}.tmp", Uuid::new_v4()));
                let mut file = private_file(&temporary, true)?;
                file.write_all(bytes)?;
                file.sync_all()?;
                drop(file);
                fs::rename(&temporary, final_path)?;
                tx.execute(
                    "INSERT INTO objects VALUES(?,?)",
                    params![sha, bytes.len() as i64],
                )?;
            }
        }
        let id = Uuid::new_v4().to_string();
        let size = data.map_or(0, |v| v.len() as u64);
        tx.execute(
            "INSERT INTO outbox(id,captured_at,source_kind,text,sha256,size) VALUES(?,?,?,?,?,?)",
            params![id, captured_at, source_kind, text, sha, size as i64],
        )?;
        tx.execute(
            "INSERT OR REPLACE INTO state VALUES('last_capture_at',?)",
            [captured_at],
        )?;
        tx.commit()?;
        self.event(&id)?.context("事件提交失败")
    }

    pub fn event(&self, id: &str) -> Result<Option<Event>> {
        Ok(self
            .connect()?
            .query_row("SELECT * FROM outbox WHERE id=?", [id], |row| {
                event_row(row, &self.root)
            })
            .optional()?)
    }

    pub fn next_due(&self, timestamp: i64) -> Result<Option<Event>> {
        Ok(self
            .connect()?
            .query_row(
                "SELECT * FROM outbox WHERE next_attempt_at<=? ORDER BY captured_at,id LIMIT 1",
                [timestamp],
                |row| event_row(row, &self.root),
            )
            .optional()?)
    }

    pub fn retry(&self, id: &str, error: &str, timestamp: i64) -> Result<()> {
        let conn = self.connect()?;
        let attempts: u32 =
            conn.query_row("SELECT attempts FROM outbox WHERE id=?", [id], |r| r.get(0))?;
        let delay = (5_i64 * 2_i64.pow((attempts + 1).min(10))).min(3600);
        conn.execute(
            "UPDATE outbox SET attempts=attempts+1,next_attempt_at=?,last_error=? WHERE id=?",
            params![timestamp + delay, error, id],
        )?;
        self.set_state("last_error", error)?;
        Ok(())
    }

    pub fn make_due(&self) -> Result<()> {
        self.connect()?
            .execute("UPDATE outbox SET next_attempt_at=0", [])?;
        Ok(())
    }

    pub fn acknowledge(&self, id: &str, acknowledged_id: &str) -> Result<()> {
        if id != acknowledged_id {
            bail!("服务端确认与事件ID不一致，保留本地事件");
        }
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        tx.execute(
            "INSERT OR IGNORE INTO receipts VALUES(?,?)",
            params![id, Utc::now().to_rfc3339()],
        )?;
        tx.execute("DELETE FROM outbox WHERE id=?", [id])?;
        tx.commit()?;
        self.collect_garbage()?;
        Ok(())
    }

    pub fn collect_garbage(&self) -> Result<()> {
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let live: Vec<String> = {
            let mut stmt =
                tx.prepare("SELECT DISTINCT sha256 FROM outbox WHERE sha256 IS NOT NULL")?;
            stmt.query_map([], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?
        };
        for entry in fs::read_dir(self.root.join("objects"))? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if entry.file_type()?.is_file() && !live.contains(&name) {
                fs::remove_file(entry.path())?;
            }
        }
        tx.execute("DELETE FROM objects WHERE sha256 NOT IN (SELECT sha256 FROM outbox WHERE sha256 IS NOT NULL)", [])?;
        tx.execute(
            "DELETE FROM receipts WHERE confirmed_at<?",
            [(Utc::now() - chrono::Duration::days(30)).to_rfc3339()],
        )?;
        tx.commit()?;
        Ok(())
    }

    pub fn set_state(&self, key: &str, value: &str) -> Result<()> {
        self.connect()?
            .execute("INSERT OR REPLACE INTO state VALUES(?,?)", [key, value])?;
        Ok(())
    }

    pub fn state(&self, key: &str) -> Result<Option<String>> {
        Ok(self
            .connect()?
            .query_row("SELECT value FROM state WHERE key=?", [key], |r| r.get(0))
            .optional()?)
    }

    pub fn status(&self) -> Result<Status> {
        let conn = self.connect()?;
        Ok(Status {
            pending_count: conn.query_row("SELECT count(*) FROM outbox", [], |r| r.get::<_,i64>(0))? as u64,
            confirmed_count: conn.query_row("SELECT count(*) FROM receipts", [], |r| r.get::<_,i64>(0))? as u64,
            cache_bytes: conn.query_row("SELECT coalesce((SELECT sum(size) FROM objects),0)+coalesce((SELECT sum(length(CAST(text AS BLOB))) FROM outbox),0)", [], |r| r.get::<_,i64>(0))? as u64,
            local_paused: self.state("local_paused")?.as_deref()==Some("true"),
            remote_paused: self.state("remote_paused")?.as_deref()==Some("true"),
            revoked: self.state("revoked")?.as_deref()==Some("true"),
            capture_status: self.state("capture_status")?.unwrap_or_else(|| "waiting".into()),
            last_capture_at: self.state("last_capture_at")?, last_error: self.state("last_error")?,
            capture_interval_seconds: self.state("capture_interval_seconds")?.and_then(|s|s.parse().ok()).unwrap_or(300),
            cache_limit_bytes: self.state("cache_limit_bytes")?.and_then(|s|s.parse().ok()).unwrap_or(1024*1024*1024),
        })
    }
}
