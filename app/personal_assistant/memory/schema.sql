PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    command TEXT,
    intent TEXT,
    state TEXT NOT NULL,
    summary TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    interrupted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    risk INTEGER NOT NULL,
    state TEXT NOT NULL,
    message TEXT,
    verified INTEGER,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_steps_task ON steps(task_id, step_index);

CREATE TABLE IF NOT EXISTS training_examples (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    label TEXT NOT NULL,
    source TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
