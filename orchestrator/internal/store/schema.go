package store

// schemaVersion is stored in meta. A store written under any other version is refused, with what to
// do about it: nothing has been released, so the stored format changes in place and old rows are not
// upgraded (docs/contributing/process-boundaries.md). Version 4 added report kinds and numbers, the
// stall and usage fields of a run, and the reminders. Version 5 took the worktree off an agent and
// renamed its provider conversation id to a resume token. Version 6 gave a run the agents it may use. Version 7 gave a finished run the working copies it kept.
const schemaVersion = 7

// The schema. It follows the draft in the design notes, with these deliberate differences:
//
//   - items and turns are keyed by (agent_id, ordinal) and clustered on it, so the newest page of an
//     agent is one range read of adjacent rows. An update names the same ordinal, and the id must
//     match, which the unique key and the WHERE of the upsert enforce without an index on the id.
//   - tables carry the status as the core's own word, so the file reads without a decoder table.
//   - messages keep seq, the sequence of their first event, because a message changes in place and
//     its place in the order must not.
//   - events keep command_id and at as columns for the log's own queries, and the body is the
//     bytes wire.EncodeEvent made, the same bytes every other table keeps.
//   - checkpoint holds the events a core.State.Checkpoint made, in generations: Compact writes the
//     next generation beside the one in force and switches meta to it with one small write, so the
//     writer is never held for the length of a checkpoint. pruned remembers what Compact dropped,
//     so a reader asking for dropped events is told and not given a silent gap.
//   - an index serves one read and puts its rows in the order the read returns, so no screen read
//     sorts: a test asks SQLite for each plan and rejects a temporary B-tree.
//
// Every table is WITHOUT ROWID where its key is not an integer, so a lookup by key is one B-tree
// walk.
const schema = `
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
  sequence   INTEGER PRIMARY KEY,
  run_id     TEXT NOT NULL,
  command_id TEXT NOT NULL,
  at         INTEGER NOT NULL,
  body       BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, sequence);

CREATE TABLE IF NOT EXISTS command_receipts (
  command_id TEXT PRIMARY KEY,
  sequence   INTEGER NOT NULL,
  result     BLOB NOT NULL) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS outbox (
  id        INTEGER PRIMARY KEY,
  effect_id TEXT NOT NULL,
  effect    BLOB NOT NULL,
  state     INTEGER NOT NULL,
  run_after INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, run_after);
CREATE INDEX IF NOT EXISTS outbox_order ON outbox(state, id, run_after);

CREATE TABLE IF NOT EXISTS runs (
  id               TEXT PRIMARY KEY,
  status           TEXT NOT NULL,
  updated_sequence INTEGER NOT NULL,
  body             BLOB NOT NULL) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS runs_status ON runs(status, updated_sequence);

CREATE TABLE IF NOT EXISTS agents (
  id               TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL,
  parent_id        TEXT NOT NULL,
  status           TEXT NOT NULL,
  updated_sequence INTEGER NOT NULL,
  body             BLOB NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS agents_run ON agents(run_id);

CREATE TABLE IF NOT EXISTS turns (
  agent_id         TEXT NOT NULL,
  ordinal          INTEGER NOT NULL,
  id               TEXT NOT NULL,
  run_id           TEXT NOT NULL,
  started_at       INTEGER NOT NULL,
  ended_at         INTEGER,
  updated_sequence INTEGER NOT NULL,
  body             BLOB NOT NULL,
  PRIMARY KEY (agent_id, ordinal)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS turns_window ON turns(run_id, started_at, id, ended_at);

CREATE TABLE IF NOT EXISTS items (
  agent_id         TEXT NOT NULL,
  ordinal          INTEGER NOT NULL,
  id               TEXT NOT NULL,
  turn_id          TEXT NOT NULL,
  updated_sequence INTEGER NOT NULL,
  body             BLOB NOT NULL,
  PRIMARY KEY (agent_id, ordinal)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS messages (
  id               TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL,
  to_agent_id      TEXT NOT NULL,
  delivery         TEXT NOT NULL,
  seq              INTEGER NOT NULL,
  updated_sequence INTEGER NOT NULL,
  body             BLOB NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS messages_run ON messages(run_id, seq);

CREATE TABLE IF NOT EXISTS checkpoint (gen INTEGER NOT NULL, idx INTEGER NOT NULL, body BLOB NOT NULL, PRIMARY KEY (gen, idx)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS pruned (run_id TEXT PRIMARY KEY, through INTEGER NOT NULL) WITHOUT ROWID;
`

// screenQueries are the reads that serve the screens, with the shape each must keep: a search on an
// index and never a scan. A test asks SQLite for each plan.
var screenQueries = map[string]string{
	"snapshot runs":     querySnapshotRuns,
	"snapshot agents":   querySnapshotAgents,
	"events of a run":   queryRunEvents,
	"events of all":     queryAllEvents,
	"items page":        queryItems,
	"messages of a run": queryMessages,
	"turns in window":   queryTurns,
	"receipt":           `SELECT result FROM command_receipts WHERE command_id = ?`,
	"claim":             queryClaim,
	"checkpoint":        queryCheckpoint,
	"next due":          queryNextDue,
}

const (
	querySnapshotRuns   = `SELECT body FROM runs WHERE status = ? ORDER BY updated_sequence DESC LIMIT ?`
	querySnapshotAgents = `SELECT body FROM agents WHERE run_id = ?`
	queryRunEvents      = `SELECT body FROM events WHERE run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?`
	queryAllEvents      = `SELECT body FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?`
	queryItems          = `SELECT body FROM items WHERE agent_id = ? AND ordinal < ? ORDER BY ordinal DESC LIMIT ?`
	queryMessages       = `SELECT body, seq FROM messages WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?`
	// The turns that overlap a window, in start order. The index holds started_at in order and
	// ended_at beside it, so the overlap test runs on the index entry and only a turn that passes is
	// read. INDEXED BY keeps the planner from choosing another index and sorting.
	queryTurns = `SELECT body, started_at, id FROM turns INDEXED BY turns_window
WHERE run_id = ?1 AND started_at < ?3 AND (ended_at IS NULL OR ended_at > ?2)
ORDER BY started_at, id LIMIT ?4`
	// The oldest ready effects first. outbox_order walks unclaimed rows in id order and tests
	// run_after on the entry: only timers not yet due are skipped, and they are few.
	queryClaim      = `SELECT id, effect FROM outbox INDEXED BY outbox_order WHERE state = 0 AND run_after <= ? ORDER BY id LIMIT ?`
	queryCheckpoint = `SELECT body FROM checkpoint WHERE gen = ? ORDER BY idx`
	queryNextDue    = `SELECT MIN(run_after) FROM outbox INDEXED BY outbox_due WHERE state = 0 AND run_after > 0`
)
