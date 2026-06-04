# Noviello Funil Saúde — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python service that receives Jurichat webhooks, attends WhatsApp leads via Claude using the `noviello-saude-suplementar` skill, halts on close-intent or handoff signals, and runs scheduled follow-ups using CRM tag rules.

**Architecture:** Single FastAPI service with 5 modules (`webhooks`, `state`, `brain`, `outbound`, `scheduler`). SQLite for state persistence. systemd manages the FastAPI process + an hourly timer for the follow-up scheduler. Nginx terminates TLS in front. All external state (conversation history) is pulled live from Jurichat — we only persist lead state machine.

**Tech Stack:** Python 3.11 · FastAPI · SQLite (stdlib `sqlite3`) · `httpx` · `anthropic` SDK · `pydantic-settings` · `pytest` + `respx` · `uv` for dep management. Deploy: Ubuntu 22.04 VPS Hostinger · systemd · nginx · Let's Encrypt.

---

## File Map

```
noviello-funil-saude/
├── pyproject.toml                          [Task 1]
├── README.md                               [Task 17]
├── src/noviello_funil/
│   ├── __init__.py                         [Task 1]
│   ├── config.py                           [Task 2]
│   ├── db.py                               [Task 3]
│   ├── state.py                            [Task 4, 5]
│   ├── outbound.py                         [Task 6, 7, 8]
│   ├── brain.py                            [Task 10]
│   ├── webhooks.py                         [Task 11, 12]
│   ├── scheduler.py                        [Task 13]
│   ├── main.py                             [Task 14]
│   └── skills/saude_suplementar.md         [Task 9]
├── tests/
│   ├── __init__.py                         [Task 1]
│   ├── conftest.py                         [Task 3]
│   ├── unit/
│   │   ├── test_state.py                   [Task 4, 5]
│   │   ├── test_outbound.py                [Task 6, 7, 8]
│   │   └── test_brain.py                   [Task 10]
│   └── integration/
│       ├── test_webhooks_flow.py           [Task 11, 12]
│       └── test_scheduler_flow.py          [Task 13]
├── deploy/
│   ├── noviello-funil.service              [Task 16]
│   ├── noviello-followup.service           [Task 16]
│   ├── noviello-followup.timer             [Task 16]
│   └── nginx.conf                          [Task 16]
└── scripts/
    └── smoke.sh                            [Task 15]
```

**File ownership rule:** each file is touched by 1–3 tasks, never more. If a later task needs to modify an earlier file beyond what's planned, that's a signal to re-plan, not silently expand the task.

---

### Task 1: Project bootstrap

**Files:**
- Create: `pyproject.toml`
- Create: `src/noviello_funil/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Verify `uv` is installed**

Run: `uv --version`
Expected: prints a version like `uv 0.x.x`. If not installed, install per https://docs.astral.sh/uv/getting-started/installation/

- [ ] **Step 2: Create `pyproject.toml`**

```toml
[project]
name = "noviello-funil-saude"
version = "0.1.0"
description = "Atendente IA Saúde — Jurichat <-> Claude"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "httpx>=0.27",
    "anthropic>=0.40",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "python-dateutil>=2.9",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "respx>=0.21",
    "ruff>=0.7",
]

[project.scripts]
noviello-followup = "noviello_funil.scheduler:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/noviello_funil"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
python_files = ["test_*.py"]
addopts = "-ra -q"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "SIM"]
ignore = ["E501"]
```

- [ ] **Step 3: Create empty `__init__.py` files**

Create these three empty files:
- `src/noviello_funil/__init__.py`
- `tests/__init__.py`
- `tests/unit/__init__.py`
- `tests/integration/__init__.py`

- [ ] **Step 4: Create a smoke test that proves the project loads**

Create `tests/test_smoke.py`:

```python
"""Sanity test that the package is importable."""


def test_package_importable():
    import noviello_funil  # noqa: F401
```

- [ ] **Step 5: Install deps and verify**

Run (PowerShell): `uv sync --all-extras`
Expected: creates `.venv/`, downloads deps, prints "Installed N packages"

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS — `test_package_importable PASSED`

- [ ] **Step 6: Commit**

```powershell
git add pyproject.toml uv.lock src/ tests/
git commit -m "feat: project bootstrap with uv + pytest"
```

---

### Task 2: Config module (env vars)

**Files:**
- Create: `src/noviello_funil/config.py`
- Create: `tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_config.py`:

```python
"""Tests for the typed settings loader."""

import os

import pytest

from noviello_funil.config import Settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("JURICHAT_API_KEY", "jk-test")
    monkeypatch.setenv("JURICHAT_WEBHOOK_SECRET", "whsec-test")
    monkeypatch.setenv("NOTIFICACAO_TELEFONE", "5511999999999")

    s = Settings()

    assert s.anthropic_api_key == "sk-test"
    assert s.jurichat_api_key == "jk-test"
    assert s.jurichat_webhook_secret == "whsec-test"
    assert s.notificacao_telefone == "5511999999999"
    assert s.jurichat_base_url == "https://api.jurichat.com"
    assert s.max_turnos_por_lead == 20
    assert s.followup_1_apos_horas == 48
    assert s.followup_2_apos_horas == 72
    assert s.encerramento_apos_horas == 24
    assert s.anthropic_model.startswith("claude-")


def test_settings_missing_required_fails(monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "JURICHAT_API_KEY",
        "JURICHAT_WEBHOOK_SECRET",
        "NOTIFICACAO_TELEFONE",
    ]:
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(Exception):  # pydantic ValidationError
        Settings()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'noviello_funil.config'`

- [ ] **Step 3: Implement Settings**

Create `src/noviello_funil/config.py`:

```python
"""Typed environment-backed settings.

All secrets and tunables come from environment variables (or `.env` file
during development). Never hardcode values that vary across environments.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # External APIs
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-5"
    jurichat_api_key: str
    jurichat_webhook_secret: str
    jurichat_base_url: str = "https://api.jurichat.com"

    # Mario's WhatsApp number for notifications (E.164, digits only)
    notificacao_telefone: str

    # SQLite
    database_path: str = "./data/noviello.db"

    # FastAPI
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"

    # Throttling & limits
    max_turnos_por_lead: int = 20
    throttle_msg_por_segundo: float = 1.0

    # Follow-up timers (horas)
    followup_1_apos_horas: int = Field(default=48, ge=1)
    followup_2_apos_horas: int = Field(default=72, ge=1)
    encerramento_apos_horas: int = Field(default=24, ge=1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: PASS — both tests green

- [ ] **Step 5: Commit**

```powershell
git add src/noviello_funil/config.py tests/unit/test_config.py
git commit -m "feat(config): typed settings via pydantic-settings"
```

---

### Task 3: DB connection + migration

**Files:**
- Create: `src/noviello_funil/db.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_db.py`

- [ ] **Step 1: Write fixtures shared across tests**

Create `tests/conftest.py`:

```python
"""Shared test fixtures."""

from collections.abc import Iterator

import pytest

from noviello_funil.db import connect, run_migrations


@pytest.fixture
def db_conn() -> Iterator:
    """In-memory SQLite connection with schema applied."""
    conn = connect(":memory:")
    run_migrations(conn)
    yield conn
    conn.close()
```

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_db.py`:

```python
"""Tests for sqlite connection and migrations."""

import sqlite3

from noviello_funil.db import connect, run_migrations


def test_connect_returns_row_factory():
    conn = connect(":memory:")
    assert conn.row_factory is sqlite3.Row
    conn.close()


def test_migrations_create_three_tables(db_conn):
    cursor = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [r["name"] for r in cursor.fetchall()]
    assert "leads" in tables
    assert "transicoes" in tables
    assert "webhooks_recebidos" in tables


def test_migrations_are_idempotent(db_conn):
    # Running migrations again should not raise
    run_migrations(db_conn)
    run_migrations(db_conn)


def test_leads_table_has_required_columns(db_conn):
    cursor = db_conn.execute("PRAGMA table_info(leads)")
    cols = {r["name"] for r in cursor.fetchall()}
    expected = {
        "id", "jurichat_lead_id", "jurichat_conversation_id",
        "contato_telefone", "contato_nome", "estado", "turnos",
        "ultima_msg_lead_em", "proxima_acao_em", "erro_atual",
        "criado_em", "atualizado_em",
    }
    assert expected.issubset(cols)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'noviello_funil.db'`

- [ ] **Step 4: Implement db module**

Create `src/noviello_funil/db.py`:

```python
"""SQLite connection and schema migrations.

Single migration block — applied idempotently on every startup. No
migration versioning needed for an MVP with a fixed schema.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    jurichat_lead_id         TEXT NOT NULL UNIQUE,
    jurichat_conversation_id TEXT NOT NULL,
    contato_telefone         TEXT NOT NULL,
    contato_nome             TEXT,
    estado                   TEXT NOT NULL,
    turnos                   INTEGER NOT NULL DEFAULT 0,
    ultima_msg_lead_em       TEXT,
    proxima_acao_em          TEXT,
    erro_atual               TEXT,
    criado_em                TEXT NOT NULL DEFAULT (datetime('now')),
    atualizado_em            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_leads_proxima_acao
    ON leads(proxima_acao_em)
    WHERE proxima_acao_em IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_leads_estado ON leads(estado);

CREATE TABLE IF NOT EXISTS transicoes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL REFERENCES leads(id),
    estado_anterior TEXT,
    estado_novo     TEXT NOT NULL,
    motivo          TEXT,
    payload_json    TEXT,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_transicoes_lead ON transicoes(lead_id);

CREATE TABLE IF NOT EXISTS webhooks_recebidos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fonte           TEXT NOT NULL,
    evento_id       TEXT NOT NULL,
    hash_payload    TEXT NOT NULL,
    recebido_em     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(fonte, evento_id)
);
"""


def connect(database_path: str) -> sqlite3.Connection:
    """Open SQLite connection with sensible defaults for this app."""
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        database_path,
        timeout=30,
        isolation_level=None,  # autocommit; we use explicit transactions
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema. Idempotent — uses IF NOT EXISTS everywhere."""
    conn.executescript(SCHEMA)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_db.py -v`
Expected: PASS — 4 tests green

- [ ] **Step 6: Commit**

```powershell
git add src/noviello_funil/db.py tests/conftest.py tests/unit/test_db.py
git commit -m "feat(db): sqlite connection + idempotent migrations"
```

---

### Task 4: State module — repository (CRUD)

**Files:**
- Create: `src/noviello_funil/state.py`
- Create: `tests/unit/test_state.py`

- [ ] **Step 1: Write the failing test for lead CRUD + idempotency**

Create `tests/unit/test_state.py`:

```python
"""Tests for the state repository layer."""

import pytest

from noviello_funil.state import (
    Estado,
    create_lead_if_absent,
    get_lead_by_conversation,
    is_webhook_processed,
    mark_webhook_processed,
)


def test_create_lead_if_absent_creates_new(db_conn):
    lead = create_lead_if_absent(
        db_conn,
        jurichat_lead_id="L-1",
        jurichat_conversation_id="C-1",
        contato_telefone="5511999999999",
        contato_nome="Maria",
    )
    assert lead["id"] is not None
    assert lead["jurichat_lead_id"] == "L-1"
    assert lead["estado"] == Estado.EM_CONVERSA
    assert lead["turnos"] == 0


def test_create_lead_if_absent_returns_existing(db_conn):
    first = create_lead_if_absent(
        db_conn, "L-1", "C-1", "5511...", "Maria"
    )
    second = create_lead_if_absent(
        db_conn, "L-1", "C-1", "5511...", "Maria"
    )
    assert first["id"] == second["id"]


def test_get_lead_by_conversation_returns_none_when_absent(db_conn):
    assert get_lead_by_conversation(db_conn, "C-NONE") is None


def test_get_lead_by_conversation_returns_lead(db_conn):
    create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead is not None
    assert lead["jurichat_lead_id"] == "L-1"


def test_webhook_idempotency_first_time_returns_false(db_conn):
    assert is_webhook_processed(db_conn, "jurichat", "evt-1") is False


def test_webhook_idempotency_after_marking_returns_true(db_conn):
    mark_webhook_processed(db_conn, "jurichat", "evt-1", "hash-1")
    assert is_webhook_processed(db_conn, "jurichat", "evt-1") is True


def test_webhook_marking_is_idempotent_no_raise(db_conn):
    mark_webhook_processed(db_conn, "jurichat", "evt-1", "hash-1")
    # Re-marking the same event must not raise
    mark_webhook_processed(db_conn, "jurichat", "evt-1", "hash-1")


def test_estado_constants():
    # Make sure all 6 spec'd states exist as constants
    assert Estado.EM_CONVERSA == "em_conversa"
    assert Estado.FOLLOW_UP_1_ENVIADO == "follow_up_1_enviado"
    assert Estado.FOLLOW_UP_2_ENVIADO == "follow_up_2_enviado"
    assert Estado.ENCERRADO_SEM_RESPOSTA == "encerrado_sem_resposta"
    assert Estado.AGUARDANDO_HUMANO == "aguardando_humano"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'noviello_funil.state'`

- [ ] **Step 3: Implement state repository (CRUD + idempotency)**

Create `src/noviello_funil/state.py`:

```python
"""State repository: the ONLY module that reads/writes SQLite.

Estado is a string enum kept as plain TEXT in the DB (no CHECK constraint
to keep migrations forgiving). All transitions go through explicit
functions defined here — never UPDATE estado from outside this module.
"""

import sqlite3
from dataclasses import dataclass
from typing import Final


class Estado:
    """All valid lead states. Used as string constants in DB."""
    EM_CONVERSA: Final = "em_conversa"
    FOLLOW_UP_1_ENVIADO: Final = "follow_up_1_enviado"
    FOLLOW_UP_2_ENVIADO: Final = "follow_up_2_enviado"
    ENCERRADO_SEM_RESPOSTA: Final = "encerrado_sem_resposta"
    AGUARDANDO_HUMANO: Final = "aguardando_humano"


# Estados em que Claude ainda pode atuar (responder/avaliar nova msg)
ESTADOS_ATIVOS_CLAUDE: Final = frozenset({
    Estado.EM_CONVERSA,
    Estado.FOLLOW_UP_1_ENVIADO,
    Estado.FOLLOW_UP_2_ENVIADO,
    Estado.ENCERRADO_SEM_RESPOSTA,
})


@dataclass
class Lead:
    """In-memory lead row. Use sqlite3.Row directly when possible —
    this dataclass exists for type-safe parameter passing."""
    id: int
    jurichat_lead_id: str
    jurichat_conversation_id: str
    contato_telefone: str
    contato_nome: str | None
    estado: str
    turnos: int
    ultima_msg_lead_em: str | None
    proxima_acao_em: str | None
    erro_atual: str | None


def create_lead_if_absent(
    conn: sqlite3.Connection,
    jurichat_lead_id: str,
    jurichat_conversation_id: str,
    contato_telefone: str,
    contato_nome: str | None,
) -> sqlite3.Row:
    """Insert a new lead in EM_CONVERSA state if jurichat_lead_id is new.

    Returns the existing or newly-created row.
    """
    existing = conn.execute(
        "SELECT * FROM leads WHERE jurichat_lead_id = ?",
        (jurichat_lead_id,),
    ).fetchone()
    if existing is not None:
        return existing

    conn.execute(
        """
        INSERT INTO leads (
            jurichat_lead_id, jurichat_conversation_id,
            contato_telefone, contato_nome, estado, turnos
        ) VALUES (?, ?, ?, ?, ?, 0)
        """,
        (
            jurichat_lead_id,
            jurichat_conversation_id,
            contato_telefone,
            contato_nome,
            Estado.EM_CONVERSA,
        ),
    )
    return conn.execute(
        "SELECT * FROM leads WHERE jurichat_lead_id = ?",
        (jurichat_lead_id,),
    ).fetchone()


def get_lead_by_conversation(
    conn: sqlite3.Connection, jurichat_conversation_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM leads WHERE jurichat_conversation_id = ?",
        (jurichat_conversation_id,),
    ).fetchone()


def get_lead_by_id(
    conn: sqlite3.Connection, lead_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()


def is_webhook_processed(
    conn: sqlite3.Connection, fonte: str, evento_id: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM webhooks_recebidos WHERE fonte = ? AND evento_id = ?",
        (fonte, evento_id),
    ).fetchone()
    return row is not None


def mark_webhook_processed(
    conn: sqlite3.Connection, fonte: str, evento_id: str, hash_payload: str,
) -> None:
    """Insert into webhooks_recebidos. Silently no-ops on duplicate."""
    conn.execute(
        """
        INSERT OR IGNORE INTO webhooks_recebidos (fonte, evento_id, hash_payload)
        VALUES (?, ?, ?)
        """,
        (fonte, evento_id, hash_payload),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_state.py -v`
Expected: PASS — 8 tests green

- [ ] **Step 5: Commit**

```powershell
git add src/noviello_funil/state.py tests/unit/test_state.py
git commit -m "feat(state): lead CRUD + webhook idempotency"
```

---

### Task 5: State module — transitions

**Files:**
- Modify: `src/noviello_funil/state.py` (append transition functions)
- Modify: `tests/unit/test_state.py` (append transition tests)

- [ ] **Step 1: Write the failing tests for transitions**

Append to `tests/unit/test_state.py`:

```python
from noviello_funil.state import (
    bump_turnos,
    list_leads_vencidos,
    record_lead_message_received,
    register_error,
    transicao,
)


def test_transicao_updates_estado_and_logs(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    transicao(
        db_conn, lead["id"], Estado.AGUARDANDO_HUMANO,
        motivo="claude_propor", payload={"acao": "propor"},
    )

    updated = get_lead_by_conversation(db_conn, "C-1")
    assert updated["estado"] == Estado.AGUARDANDO_HUMANO

    transicoes = db_conn.execute(
        "SELECT * FROM transicoes WHERE lead_id = ?", (lead["id"],)
    ).fetchall()
    assert len(transicoes) == 1
    assert transicoes[0]["estado_anterior"] == Estado.EM_CONVERSA
    assert transicoes[0]["estado_novo"] == Estado.AGUARDANDO_HUMANO
    assert transicoes[0]["motivo"] == "claude_propor"


def test_bump_turnos_increments(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    bump_turnos(db_conn, lead["id"])
    bump_turnos(db_conn, lead["id"])
    updated = get_lead_by_conversation(db_conn, "C-1")
    assert updated["turnos"] == 2


def test_record_lead_message_received_updates_timestamp(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    record_lead_message_received(db_conn, lead["id"], proxima_acao_horas=48)
    updated = get_lead_by_conversation(db_conn, "C-1")
    assert updated["ultima_msg_lead_em"] is not None
    assert updated["proxima_acao_em"] is not None


def test_record_lead_message_resets_turnos_if_reopening(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    bump_turnos(db_conn, lead["id"])
    bump_turnos(db_conn, lead["id"])
    transicao(db_conn, lead["id"], Estado.ENCERRADO_SEM_RESPOSTA, motivo="timer")

    # Lead reopens — turnos must reset
    record_lead_message_received(
        db_conn, lead["id"], proxima_acao_horas=48, reset_turnos=True,
    )
    updated = get_lead_by_conversation(db_conn, "C-1")
    assert updated["turnos"] == 0


def test_register_error_sets_flag(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    register_error(db_conn, lead["id"], "claude_invalid_json")
    updated = get_lead_by_conversation(db_conn, "C-1")
    assert updated["erro_atual"] == "claude_invalid_json"


def test_list_leads_vencidos_returns_only_due_and_active(db_conn):
    import datetime

    past = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    future = (datetime.datetime.utcnow() + datetime.timedelta(hours=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # Lead 1: due, em_conversa → should appear
    conn = db_conn
    conn.execute(
        """INSERT INTO leads (jurichat_lead_id, jurichat_conversation_id,
                              contato_telefone, estado, proxima_acao_em)
           VALUES (?, ?, ?, ?, ?)""",
        ("L-due", "C-due", "5511...", Estado.EM_CONVERSA, past),
    )
    # Lead 2: not yet due
    conn.execute(
        """INSERT INTO leads (jurichat_lead_id, jurichat_conversation_id,
                              contato_telefone, estado, proxima_acao_em)
           VALUES (?, ?, ?, ?, ?)""",
        ("L-future", "C-future", "5511...", Estado.EM_CONVERSA, future),
    )
    # Lead 3: due but in terminal state → should NOT appear
    conn.execute(
        """INSERT INTO leads (jurichat_lead_id, jurichat_conversation_id,
                              contato_telefone, estado, proxima_acao_em)
           VALUES (?, ?, ?, ?, ?)""",
        ("L-handed", "C-handed", "5511...", Estado.AGUARDANDO_HUMANO, past),
    )

    vencidos = list_leads_vencidos(conn)
    ids = {row["jurichat_lead_id"] for row in vencidos}
    assert "L-due" in ids
    assert "L-future" not in ids
    assert "L-handed" not in ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_state.py -v`
Expected: FAIL — `ImportError: cannot import name 'transicao' from 'noviello_funil.state'`

- [ ] **Step 3: Append transition functions to state.py**

Append to `src/noviello_funil/state.py`:

```python
import json
from datetime import datetime, timedelta
from typing import Any


# --- Transitions and updates ---------------------------------------------

def transicao(
    conn: sqlite3.Connection,
    lead_id: int,
    estado_novo: str,
    *,
    motivo: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Atomic state transition: update lead.estado AND insert transicoes row.

    Always go through this function — never naked UPDATE estado.
    """
    current = conn.execute(
        "SELECT estado FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    if current is None:
        raise ValueError(f"Lead {lead_id} not found")

    estado_anterior = current["estado"]
    payload_json = json.dumps(payload) if payload is not None else None

    conn.execute("BEGIN")
    try:
        conn.execute(
            "UPDATE leads SET estado = ?, atualizado_em = datetime('now') WHERE id = ?",
            (estado_novo, lead_id),
        )
        conn.execute(
            """
            INSERT INTO transicoes (lead_id, estado_anterior, estado_novo, motivo, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (lead_id, estado_anterior, estado_novo, motivo, payload_json),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def bump_turnos(conn: sqlite3.Connection, lead_id: int) -> None:
    conn.execute(
        "UPDATE leads SET turnos = turnos + 1, atualizado_em = datetime('now') "
        "WHERE id = ?",
        (lead_id,),
    )


def record_lead_message_received(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    proxima_acao_horas: int,
    reset_turnos: bool = False,
) -> None:
    """Mark that a new message from the lead arrived.

    Updates ultima_msg_lead_em=now, schedules proxima_acao_em=now+H hours.
    If reset_turnos=True, also resets turnos to 0 (used when reopening
    from encerrado_sem_resposta).
    """
    proxima = (datetime.utcnow() + timedelta(hours=proxima_acao_horas)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    if reset_turnos:
        conn.execute(
            """
            UPDATE leads SET
                ultima_msg_lead_em = datetime('now'),
                proxima_acao_em = ?,
                turnos = 0,
                atualizado_em = datetime('now')
            WHERE id = ?
            """,
            (proxima, lead_id),
        )
    else:
        conn.execute(
            """
            UPDATE leads SET
                ultima_msg_lead_em = datetime('now'),
                proxima_acao_em = ?,
                atualizado_em = datetime('now')
            WHERE id = ?
            """,
            (proxima, lead_id),
        )


def schedule_next_action(
    conn: sqlite3.Connection, lead_id: int, horas: int,
) -> None:
    """Set proxima_acao_em = now + horas. Used by scheduler after sending follow-up."""
    proxima = (datetime.utcnow() + timedelta(hours=horas)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "UPDATE leads SET proxima_acao_em = ?, atualizado_em = datetime('now') WHERE id = ?",
        (proxima, lead_id),
    )


def clear_next_action(conn: sqlite3.Connection, lead_id: int) -> None:
    conn.execute(
        "UPDATE leads SET proxima_acao_em = NULL, atualizado_em = datetime('now') "
        "WHERE id = ?",
        (lead_id,),
    )


def register_error(
    conn: sqlite3.Connection, lead_id: int, erro: str | None,
) -> None:
    conn.execute(
        "UPDATE leads SET erro_atual = ?, atualizado_em = datetime('now') WHERE id = ?",
        (erro, lead_id),
    )


def list_leads_vencidos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Leads whose proxima_acao_em has passed AND are in a non-terminal state."""
    active_states = (
        Estado.EM_CONVERSA,
        Estado.FOLLOW_UP_1_ENVIADO,
        Estado.FOLLOW_UP_2_ENVIADO,
    )
    placeholders = ",".join("?" * len(active_states))
    return conn.execute(
        f"""
        SELECT * FROM leads
        WHERE proxima_acao_em IS NOT NULL
          AND proxima_acao_em < datetime('now')
          AND estado IN ({placeholders})
        ORDER BY proxima_acao_em ASC
        """,
        active_states,
    ).fetchall()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_state.py -v`
Expected: PASS — all 14 tests green (8 from Task 4 + 6 new)

- [ ] **Step 5: Commit**

```powershell
git add src/noviello_funil/state.py tests/unit/test_state.py
git commit -m "feat(state): transitions, turnos, scheduling helpers"
```

---

### Task 6: Outbound — base HTTP client + retry helper

**Files:**
- Create: `src/noviello_funil/outbound.py`
- Create: `tests/unit/test_outbound.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_outbound.py`:

```python
"""Tests for the outbound HTTP layer."""

import httpx
import pytest
import respx

from noviello_funil.outbound import (
    JurichatClient,
    OutboundError,
    with_retry,
)


@pytest.mark.asyncio
async def test_with_retry_succeeds_first_attempt():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        return "ok"

    result = await with_retry(op, attempts=3, base_delay=0.001)
    assert result == "ok"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_failures():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.HTTPError("boom")
        return "ok"

    result = await with_retry(op, attempts=3, base_delay=0.001)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_with_retry_gives_up_after_max():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise httpx.HTTPError("boom")

    with pytest.raises(OutboundError):
        await with_retry(op, attempts=3, base_delay=0.001)
    assert calls["n"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_outbound.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'noviello_funil.outbound'`

- [ ] **Step 3: Implement base outbound module**

Create `src/noviello_funil/outbound.py`:

```python
"""HTTP client for outbound calls to Jurichat and to send notifications to Mario.

Uses httpx.AsyncClient. All operations go through `with_retry` for
transient-failure resilience.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


class OutboundError(Exception):
    """Raised when an outbound call exhausts all retries."""


async def with_retry(
    op: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
) -> T:
    """Run async `op` with exponential backoff.

    Delays: base_delay * (3 ** attempt-1) — so 1s, 3s, 9s for default.
    Raises OutboundError if all attempts fail (preserves last httpx error
    in __cause__).
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt == attempts:
                break
            delay = base_delay * (3 ** (attempt - 1))
            logger.warning(
                "outbound_retry attempt=%d/%d delay=%.1fs err=%s",
                attempt, attempts, delay, exc,
            )
            await asyncio.sleep(delay)
    raise OutboundError(f"all {attempts} attempts failed") from last_exc


class JurichatClient:
    """Thin wrapper over Jurichat REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"x-jurichat-api-key": api_key},
        )

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_outbound.py -v`
Expected: PASS — 3 retry tests green

- [ ] **Step 5: Commit**

```powershell
git add src/noviello_funil/outbound.py tests/unit/test_outbound.py
git commit -m "feat(outbound): http client base + retry/backoff"
```

---

### Task 7: Outbound — Jurichat endpoints

**Files:**
- Modify: `src/noviello_funil/outbound.py` (append Jurichat methods)
- Modify: `tests/unit/test_outbound.py` (append endpoint tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_outbound.py`:

```python
@pytest.mark.asyncio
async def test_jurichat_send_message_calls_correct_endpoint(respx_mock):
    route = respx_mock.post(
        "https://api.jurichat.com/conversation/send-message"
    ).mock(return_value=httpx.Response(200, json={"id": "msg-1"}))

    client = JurichatClient(
        api_key="jk-test", base_url="https://api.jurichat.com",
    )
    try:
        result = await client.send_message(
            conversation_id="C-1", text="Olá Maria",
        )
    finally:
        await client.aclose()

    assert route.called
    assert result == {"id": "msg-1"}
    sent_form = route.calls.last.request.read()
    assert b'conversation_id' in sent_form
    assert b'C-1' in sent_form
    assert b"Ol" in sent_form  # accent-encoded


@pytest.mark.asyncio
async def test_jurichat_get_conversation_returns_transcript(respx_mock):
    respx_mock.get(
        "https://api.jurichat.com/conversation/C-1"
    ).mock(return_value=httpx.Response(
        200, json={
            "id": "C-1",
            "transcription": "Lead: oi\nAtendente: ola",
            "summary": "primeiro contato",
        },
    ))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        result = await client.get_conversation("C-1")
    finally:
        await client.aclose()

    assert result["transcription"].startswith("Lead:")


@pytest.mark.asyncio
async def test_jurichat_get_lead_tags_returns_list(respx_mock):
    respx_mock.get(
        "https://api.jurichat.com/crm/lead/L-1"
    ).mock(return_value=httpx.Response(
        200, json={"id": "L-1", "tags": [
            {"name": "Fazer Follow up"}, {"name": "Proposta enviada"},
        ]},
    ))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        tags = await client.get_lead_tags("L-1")
    finally:
        await client.aclose()

    assert "Fazer Follow up" in tags
    assert "Proposta enviada" in tags


@pytest.mark.asyncio
async def test_jurichat_retries_on_5xx(respx_mock):
    route = respx_mock.post(
        "https://api.jurichat.com/conversation/send-message"
    ).mock(side_effect=[
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, json={"id": "msg-ok"}),
    ])

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        result = await client.send_message("C-1", "ola", base_delay=0.001)
    finally:
        await client.aclose()

    assert route.call_count == 3
    assert result["id"] == "msg-ok"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_outbound.py -v`
Expected: FAIL — `AttributeError: 'JurichatClient' object has no attribute 'send_message'`

- [ ] **Step 3: Append Jurichat endpoint methods**

Append to `src/noviello_funil/outbound.py` (inside the `JurichatClient` class):

```python
    async def send_message(
        self,
        conversation_id: str,
        text: str,
        *,
        base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """POST /conversation/send-message (multipart/form-data)."""

        async def op() -> dict[str, Any]:
            resp = await self._client.post(
                f"{self._base_url}/conversation/send-message",
                data={"conversation_id": conversation_id, "text": text},
            )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

    async def get_conversation(
        self, conversation_id: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """GET /conversation/{id} — returns full conversation including transcription."""

        async def op() -> dict[str, Any]:
            resp = await self._client.get(
                f"{self._base_url}/conversation/{conversation_id}"
            )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

    async def get_lead_tags(
        self, lead_id: str, *, base_delay: float = 1.0,
    ) -> list[str]:
        """GET /crm/lead/{id} — returns list of tag names (empty if none).

        NOTE: exact endpoint shape pending confirmation (see spec §15.4).
        Adjust the path if Jurichat docs differ.
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.get(
                f"{self._base_url}/crm/lead/{lead_id}"
            )
            resp.raise_for_status()
            return resp.json()

        data = await with_retry(op, attempts=3, base_delay=base_delay)
        return [t["name"] for t in data.get("tags", [])]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_outbound.py -v`
Expected: PASS — 7 tests green (3 retry + 4 endpoint)

- [ ] **Step 5: Commit**

```powershell
git add src/noviello_funil/outbound.py tests/unit/test_outbound.py
git commit -m "feat(outbound): jurichat send-message, get-conversation, get-tags"
```

---

### Task 8: Outbound — Mario notification helper

**Files:**
- Modify: `src/noviello_funil/outbound.py` (append `notify_mario`)
- Modify: `tests/unit/test_outbound.py` (append notify test)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_outbound.py`:

```python
from noviello_funil.outbound import format_notification, notify_mario


def test_format_notification_fechar():
    msg = format_notification(
        tipo="fechar",
        nome="Maria",
        telefone="5511999999999",
        ultima_msg="como faço pra contratar?",
        resumo="Plano negou bariátrica",
        conversation_id="C-42",
    )
    assert msg.startswith("🔥")
    assert "Maria" in msg
    assert "5511999999999" in msg
    assert "C-42" in msg


def test_format_notification_handoff():
    msg = format_notification(
        tipo="handoff",
        nome="João",
        telefone="5511888888888",
        ultima_msg="quero falar com humano",
        resumo=None,
        motivo="pediu falar com humano",
        conversation_id="C-99",
    )
    assert msg.startswith("⚠️")
    assert "pediu falar com humano" in msg


def test_format_notification_turnos_excedidos():
    msg = format_notification(
        tipo="turnos",
        nome="Ana",
        telefone="5511777777777",
        ultima_msg="vou pensar",
        resumo=None,
        conversation_id="C-1",
    )
    assert msg.startswith("⏸")
    assert "20" in msg or "turnos" in msg.lower()


@pytest.mark.asyncio
async def test_notify_mario_sends_to_configured_number(respx_mock):
    # Notification = a send_message to the special Mario notification
    # "conversation" — Jurichat treats this as a normal outbound message.
    respx_mock.post(
        "https://api.jurichat.com/conversation/send-message"
    ).mock(return_value=httpx.Response(200, json={"id": "msg-notif"}))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        await notify_mario(
            client,
            mario_conversation_id="C-MARIO",
            mensagem="🔥 teste de notificação",
        )
    finally:
        await client.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_outbound.py -v`
Expected: FAIL — `ImportError: cannot import name 'format_notification'`

- [ ] **Step 3: Append notification helpers to outbound**

Append to `src/noviello_funil/outbound.py` (at module level, outside the class):

```python
def format_notification(
    *,
    tipo: str,
    nome: str | None,
    telefone: str,
    ultima_msg: str,
    resumo: str | None = None,
    motivo: str | None = None,
    conversation_id: str,
) -> str:
    """Format a notification message for Mario.

    tipo: 'fechar' | 'handoff' | 'turnos'
    """
    nome_label = nome or "(sem nome)"

    if tipo == "fechar":
        head = f"🔥 Lead {nome_label} ({telefone}) — QUER FECHAR"
        body = f'Última msg: "{ultima_msg}"'
        extra = f"Resumo Claude: {resumo}" if resumo else ""
    elif tipo == "handoff":
        head = f"⚠️ Lead {nome_label} ({telefone}) — PRECISA DE VOCÊ"
        body = f"Motivo: {motivo or 'não especificado'}"
        extra = f'Última msg: "{ultima_msg}"'
    elif tipo == "turnos":
        head = f"⏸ Lead {nome_label} ({telefone}) — 20 turnos sem progresso"
        body = f'Última msg: "{ultima_msg}"'
        extra = ""
    else:
        raise ValueError(f"unknown notification type: {tipo}")

    link = f"https://app.jurichat.com/conversation/{conversation_id}"

    parts = [head, body]
    if extra:
        parts.append(extra)
    parts.append(f"Link: {link}")
    return "\n".join(parts)


async def notify_mario(
    client: JurichatClient,
    *,
    mario_conversation_id: str,
    mensagem: str,
) -> None:
    """Send notification message to Mario via Jurichat.

    `mario_conversation_id` is a conversation with Mario's own number,
    pre-configured. Failures are logged but NOT raised — notifications
    are fire-and-forget per spec §9.
    """
    try:
        await client.send_message(mario_conversation_id, mensagem)
    except OutboundError as exc:
        logger.error("notify_mario failed: %s", exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_outbound.py -v`
Expected: PASS — 11 tests green

- [ ] **Step 5: Commit**

```powershell
git add src/noviello_funil/outbound.py tests/unit/test_outbound.py
git commit -m "feat(outbound): mario notification formatter + sender"
```

---

### Task 9: Skill content — system prompt for saúde

**Files:**
- Create: `src/noviello_funil/skills/__init__.py`
- Create: `src/noviello_funil/skills/saude_suplementar.md`

- [ ] **Step 1: Create the skills package marker**

Create empty `src/noviello_funil/skills/__init__.py`.

- [ ] **Step 2: Write the system prompt content**

Create `src/noviello_funil/skills/saude_suplementar.md`:

```markdown
# Atendente IA — Plano de Saúde Suplementar (Noviello Advocacia)

Você é um atendente virtual da Noviello Advocacia, especializado em
direito à saúde suplementar. Atua via WhatsApp, conversando com leads
que chegam pelo Jurichat.

## Seu papel

1. Acolher o lead com empatia (situações de saúde são sensíveis)
2. Entender a dor jurídica concreta: negativa de cobertura, reajuste
   abusivo, falsa coletivização de plano, demora em autorização
3. Coletar dados básicos: nome, plano de saúde, qual procedimento/medicação
   foi negado(a), há quanto tempo
4. Avaliar se há caso jurídico viável
5. Quando o lead manifestar intent claro de contratar (perguntar valor,
   "como faço pra começar", aceitar seguir adiante) E houver dor concreta
   identificada — decidir por `propor`

## Quando decidir cada ação

Sempre responda em JSON estrito, sem texto fora do JSON.

### acao = "responder"
- Lead ainda está se informando, tirando dúvidas, contando o caso
- Não há intent claro de contratar
- Próximo passo: continuar conversa

### acao = "propor"
- Lead perguntou valor explicitamente, OU
- Lead disse algo como "como faço pra contratar", "quero começar",
  "vamos seguir", OU
- Lead aceitou explicitamente proposta verbal
- E há dor jurídica concreta identificada
- O campo `mensagem` deve conter a proposta a enviar
- O campo `resumo_caso` deve descrever em 1–2 linhas pra Mario

### acao = "handoff"
- Lead pediu falar com humano explicitamente
- Lead virou agressivo, hostil, ou usou linguagem desrespeitosa
- Tema fora da skill (não é saúde — divórcio, trabalho, criminal etc.)
- Lead em emergência médica REAL (dor de morrer, suicídio, urgência) —
  oriente procurar pronto-socorro e marque handoff
- O campo `motivo_handoff` deve explicar em 1 linha

## Voz e estilo

- Tom profissional, cordial, claro
- Sem juridiquês — o lead é leigo
- Frases curtas, parágrafos curtos
- Use "você", nunca "senhor(a)" (cliente Noviello é cliente direto, próximo)
- Evite emojis em excesso (1 por mensagem no máximo, e só quando agrega)
- Em situação delicada, valide a emoção antes de avançar

## Limites éticos OAB (CRÍTICO)

- NUNCA prometa resultado ("vai ganhar", "garante que")
- NUNCA cite valor concreto sem que Mario tenha autorizado
- NUNCA mencione casos específicos de outros clientes
- NUNCA faça comparação com outros escritórios
- Mantenha sempre tom informativo, não mercantilista

## Formato de saída

Você DEVE retornar APENAS JSON válido, neste schema, sem markdown:

```json
{
  "acao": "responder" | "propor" | "handoff",
  "mensagem": "<texto a enviar ao lead>",
  "resumo_caso": "<presente apenas se acao=propor>",
  "motivo_handoff": "<presente apenas se acao=handoff>"
}
```

Se `acao` for `responder`, omita `resumo_caso` e `motivo_handoff`.
Se `acao` for `propor`, inclua `resumo_caso`; omita `motivo_handoff`.
Se `acao` for `handoff`, inclua `motivo_handoff`; omita `resumo_caso`.

NÃO escreva nada fora do JSON. NÃO use markdown blocks (```). Apenas o
objeto JSON puro.
```

- [ ] **Step 3: Commit**

```powershell
git add src/noviello_funil/skills/
git commit -m "feat(skills): system prompt para atendente de saúde"
```

---

### Task 10: Brain — Claude integration

**Files:**
- Create: `src/noviello_funil/brain.py`
- Create: `tests/unit/test_brain.py`

> **Note for implementer:** Before implementing this task, consider invoking the `claude-api` skill for current best practices on prompt caching, model selection, and structured output. The implementation below follows the spec's prompt-engineered JSON approach (not tool use) with prompt caching enabled on the static skill content.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_brain.py`:

```python
"""Tests for the brain module — Claude prompting + parsing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.brain import (
    Decisao,
    DecisaoInvalida,
    load_skill,
    parse_decisao,
    triagem,
)


def test_load_skill_returns_nonempty_string():
    content = load_skill("saude_suplementar")
    assert "Noviello" in content
    assert len(content) > 200


def test_parse_decisao_responder():
    raw = '{"acao": "responder", "mensagem": "olá maria"}'
    d = parse_decisao(raw)
    assert d.acao == "responder"
    assert d.mensagem == "olá maria"
    assert d.resumo_caso is None
    assert d.motivo_handoff is None


def test_parse_decisao_propor():
    raw = '{"acao": "propor", "mensagem": "proposta x", "resumo_caso": "plano negou"}'
    d = parse_decisao(raw)
    assert d.acao == "propor"
    assert d.resumo_caso == "plano negou"


def test_parse_decisao_handoff():
    raw = '{"acao": "handoff", "mensagem": "vou te passar", "motivo_handoff": "pediu humano"}'
    d = parse_decisao(raw)
    assert d.acao == "handoff"
    assert d.motivo_handoff == "pediu humano"


def test_parse_decisao_unknown_acao_raises():
    raw = '{"acao": "explodir", "mensagem": "..."}'
    with pytest.raises(DecisaoInvalida):
        parse_decisao(raw)


def test_parse_decisao_invalid_json_raises():
    with pytest.raises(DecisaoInvalida):
        parse_decisao("not json at all")


def test_parse_decisao_extracts_json_from_markdown_block():
    # Sometimes Claude wraps in ``` despite instructions
    raw = '```json\n{"acao": "responder", "mensagem": "oi"}\n```'
    d = parse_decisao(raw)
    assert d.acao == "responder"


@pytest.mark.asyncio
async def test_triagem_returns_decision_on_first_call():
    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text='{"acao":"responder","mensagem":"ok"}')]
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    decisao = await triagem(
        client=fake_client,
        model="claude-sonnet-4-5",
        skill_content="SKILL",
        conversation_transcript="Lead: oi",
    )

    assert decisao.acao == "responder"
    assert decisao.mensagem == "ok"
    fake_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_triagem_retries_once_on_invalid_json():
    fake_client = MagicMock()
    bad_resp = MagicMock()
    bad_resp.content = [MagicMock(text="not json")]
    good_resp = MagicMock()
    good_resp.content = [MagicMock(text='{"acao":"responder","mensagem":"ok"}')]
    fake_client.messages.create = AsyncMock(side_effect=[bad_resp, good_resp])

    decisao = await triagem(
        client=fake_client,
        model="claude-sonnet-4-5",
        skill_content="SKILL",
        conversation_transcript="Lead: oi",
    )

    assert decisao.acao == "responder"
    assert fake_client.messages.create.call_count == 2


@pytest.mark.asyncio
async def test_triagem_gives_up_after_second_invalid():
    fake_client = MagicMock()
    bad_resp = MagicMock()
    bad_resp.content = [MagicMock(text="not json")]
    fake_client.messages.create = AsyncMock(return_value=bad_resp)

    with pytest.raises(DecisaoInvalida):
        await triagem(
            client=fake_client,
            model="claude-sonnet-4-5",
            skill_content="SKILL",
            conversation_transcript="Lead: oi",
        )

    assert fake_client.messages.create.call_count == 2


@pytest.mark.asyncio
async def test_followup_message_returns_text():
    from noviello_funil.brain import gerar_followup_msg

    fake_client = MagicMock()
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text="Oi Maria, retomando nosso papo...")]
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    text = await gerar_followup_msg(
        client=fake_client,
        model="claude-sonnet-4-5",
        skill_content="SKILL",
        conversation_transcript="Lead: oi (há 2 dias)",
    )

    assert "Maria" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_brain.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'noviello_funil.brain'`

- [ ] **Step 3: Implement brain module**

Create `src/noviello_funil/brain.py`:

```python
"""Brain module: Claude prompt assembly, invocation, and structured parsing.

Strategy:
- Static skill content goes in `system` with cache_control=ephemeral so
  Anthropic caches it (5-min TTL, dramatic latency/cost reduction on
  back-to-back turns of the same conversation).
- Conversation transcript pulled live from Jurichat is the dynamic part
  passed as a `user` message.
- Response is plain text expected to be JSON. We parse and validate.
  If invalid, retry once with a tightened instruction. Then give up.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SKILLS_DIR = Path(__file__).parent / "skills"

VALID_ACOES = frozenset({"responder", "propor", "handoff"})


@dataclass
class Decisao:
    acao: Literal["responder", "propor", "handoff"]
    mensagem: str
    resumo_caso: str | None = None
    motivo_handoff: str | None = None


class DecisaoInvalida(Exception):
    """Claude returned malformed JSON or unknown acao after all retries."""


def load_skill(name: str) -> str:
    """Read a skill .md file from src/noviello_funil/skills/."""
    path = SKILLS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


def parse_decisao(raw: str) -> Decisao:
    """Parse Claude's text response into a Decisao.

    Strips markdown code fences if Claude added them despite instructions.
    Raises DecisaoInvalida on any parse problem.
    """
    text = raw.strip()
    # Strip ```json ... ``` or ``` ... ``` wrappers if present
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DecisaoInvalida(f"not valid json: {exc}") from exc

    if not isinstance(data, dict):
        raise DecisaoInvalida(f"expected json object, got {type(data).__name__}")

    acao = data.get("acao")
    if acao not in VALID_ACOES:
        raise DecisaoInvalida(f"unknown acao: {acao!r}")

    mensagem = data.get("mensagem")
    if not isinstance(mensagem, str) or not mensagem.strip():
        raise DecisaoInvalida("mensagem must be non-empty string")

    return Decisao(
        acao=acao,
        mensagem=mensagem,
        resumo_caso=data.get("resumo_caso"),
        motivo_handoff=data.get("motivo_handoff"),
    )


def _build_system(skill_content: str) -> list[dict[str, Any]]:
    """System prompt with prompt caching enabled on the static skill block."""
    return [
        {
            "type": "text",
            "text": skill_content,
            "cache_control": {"type": "ephemeral"},
        }
    ]


async def triagem(
    *,
    client: Any,
    model: str,
    skill_content: str,
    conversation_transcript: str,
) -> Decisao:
    """Send the conversation to Claude and parse a Decisao.

    Retries once on invalid JSON with a stricter instruction.
    """
    user_text = (
        "Abaixo está a transcrição completa da conversa atual com o lead. "
        "Decida a próxima ação seguindo as regras da skill e responda APENAS "
        "com o objeto JSON especificado, sem texto fora dele.\n\n"
        "=== TRANSCRIÇÃO ===\n"
        f"{conversation_transcript}"
    )

    first = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=_build_system(skill_content),
        messages=[{"role": "user", "content": user_text}],
    )
    raw = first.content[0].text

    try:
        return parse_decisao(raw)
    except DecisaoInvalida:
        pass

    retry_text = (
        "Sua resposta anterior não foi JSON válido. Responda AGORA apenas com "
        "o objeto JSON especificado, sem texto antes ou depois, sem markdown."
    )
    second = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=_build_system(skill_content),
        messages=[
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": raw},
            {"role": "user", "content": retry_text},
        ],
    )
    return parse_decisao(second.content[0].text)


async def gerar_followup_msg(
    *,
    client: Any,
    model: str,
    skill_content: str,
    conversation_transcript: str,
) -> str:
    """Generate a contextual follow-up message (1st follow-up only).

    Different from `triagem`: returns plain text, not JSON. Claude is
    instructed to write a single short message to send to the lead.
    """
    user_text = (
        "O lead abaixo não respondeu há ~48h. Escreva uma mensagem curta, "
        "natural e empática para retomar a conversa, fazendo referência ao "
        "tema concreto que conversamos. NÃO repita a última mensagem nossa. "
        "Responda APENAS com o texto da mensagem a enviar, sem aspas, sem "
        "preâmbulo.\n\n"
        "=== TRANSCRIÇÃO ===\n"
        f"{conversation_transcript}"
    )

    resp = await client.messages.create(
        model=model,
        max_tokens=512,
        system=_build_system(skill_content),
        messages=[{"role": "user", "content": user_text}],
    )
    return resp.content[0].text.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_brain.py -v`
Expected: PASS — 11 tests green

- [ ] **Step 5: Commit**

```powershell
git add src/noviello_funil/brain.py tests/unit/test_brain.py
git commit -m "feat(brain): claude triagem + followup with prompt caching"
```

---

### Task 11: Webhooks — HMAC validation + idempotency + route registration

**Files:**
- Create: `src/noviello_funil/webhooks.py`
- Create: `tests/integration/test_webhooks_flow.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_webhooks_flow.py`:

```python
"""Integration tests for the webhook receiver."""

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from noviello_funil.db import connect, run_migrations
from noviello_funil.webhooks import register_webhooks


@pytest.fixture
def app():
    """FastAPI app with webhooks registered and in-memory DB."""
    conn = connect(":memory:")
    run_migrations(conn)

    fastapi_app = FastAPI()
    register_webhooks(
        fastapi_app,
        get_db=lambda: conn,
        webhook_secret="whsec-test",
        process_lead_message=lambda payload: None,  # no-op in this test
    )
    return fastapi_app


@pytest.fixture
def client(app):
    return TestClient(app)


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_returns_401_on_invalid_signature(client):
    body = b'{"event":"chat.conversation.updated","id":"e-1"}'
    r = client.post(
        "/webhooks/jurichat",
        content=body,
        headers={
            "X-JuriChat-Signature": "bad",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 401


def test_webhook_returns_200_on_valid_signature(client):
    body = b'{"event":"chat.conversation.updated","id":"e-1"}'
    sig = _sign("whsec-test", body)
    r = client.post(
        "/webhooks/jurichat",
        content=body,
        headers={"X-JuriChat-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200


def test_webhook_duplicate_event_returns_200_idempotently(client):
    body = b'{"event":"chat.conversation.updated","id":"e-dup"}'
    sig = _sign("whsec-test", body)
    headers = {"X-JuriChat-Signature": sig, "Content-Type": "application/json"}

    r1 = client.post("/webhooks/jurichat", content=body, headers=headers)
    r2 = client.post("/webhooks/jurichat", content=body, headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicated") is True


def test_webhook_responds_fast_with_background_processing(app, client):
    """The handler must respond before the background task runs."""
    called = {"n": 0}

    def slow_processor(payload):
        called["n"] += 1

    # Re-register with our spy processor
    from noviello_funil.webhooks import register_webhooks
    from noviello_funil.db import connect, run_migrations
    conn = connect(":memory:")
    run_migrations(conn)

    spy_app = FastAPI()
    register_webhooks(
        spy_app,
        get_db=lambda: conn,
        webhook_secret="whsec-test",
        process_lead_message=slow_processor,
    )

    body = b'{"event":"chat.conversation.updated","id":"e-fast"}'
    sig = _sign("whsec-test", body)
    with TestClient(spy_app) as c:
        r = c.post(
            "/webhooks/jurichat",
            content=body,
            headers={"X-JuriChat-Signature": sig, "Content-Type": "application/json"},
        )
    assert r.status_code == 200
    # After TestClient context exits, background tasks have completed
    assert called["n"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_webhooks_flow.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'noviello_funil.webhooks'`

- [ ] **Step 3: Implement webhooks module**

Create `src/noviello_funil/webhooks.py`:

```python
"""HTTP entry point: receives Jurichat webhooks.

Responsibilities:
- HMAC-SHA256 signature verification (header X-JuriChat-Signature)
- Idempotency check via webhooks_recebidos table
- 200 response in <100ms — heavy work runs in BackgroundTask
- Delegates actual processing to the injected `process_lead_message`
  callable (defined in main.py, wires up state + brain + outbound)
"""

import hashlib
import hmac
import logging
from collections.abc import Callable
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from noviello_funil.state import is_webhook_processed, mark_webhook_processed

logger = logging.getLogger(__name__)


def _verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_event_id(payload: dict[str, Any]) -> str:
    """Best-effort event id. Falls back to a payload hash."""
    return (
        payload.get("id")
        or payload.get("event_id")
        or hashlib.sha256(repr(payload).encode()).hexdigest()[:16]
    )


def register_webhooks(
    app: FastAPI,
    *,
    get_db: Callable[[], Any],
    webhook_secret: str,
    process_lead_message: Callable[[dict[str, Any]], Any],
) -> None:
    """Register POST /webhooks/jurichat on `app`."""

    @app.post("/webhooks/jurichat")
    async def jurichat_webhook(
        request: Request, background_tasks: BackgroundTasks,
    ) -> Response:
        body = await request.body()
        signature = request.headers.get("X-JuriChat-Signature")

        if not _verify_signature(webhook_secret, body, signature):
            logger.warning("webhook hmac invalid (signature=%r)", signature)
            raise HTTPException(status_code=401, detail="invalid signature")

        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"bad json: {exc}") from exc

        conn = get_db()
        event_id = _extract_event_id(payload)
        if is_webhook_processed(conn, "jurichat", event_id):
            logger.info("webhook duplicate event_id=%s", event_id)
            return Response(
                content=b'{"ok":true,"duplicated":true}',
                media_type="application/json",
            )

        hash_payload = hashlib.sha256(body).hexdigest()
        mark_webhook_processed(conn, "jurichat", event_id, hash_payload)

        background_tasks.add_task(process_lead_message, payload)

        return Response(
            content=b'{"ok":true}', media_type="application/json",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_webhooks_flow.py -v`
Expected: PASS — 4 tests green

- [ ] **Step 5: Commit**

```powershell
git add src/noviello_funil/webhooks.py tests/integration/test_webhooks_flow.py
git commit -m "feat(webhooks): hmac validation + idempotency + background dispatch"
```

---

### Task 12: Lead message processor (the actual Cenário A handler)

**Files:**
- Modify: `src/noviello_funil/webhooks.py` (append processor factory)
- Modify: `tests/integration/test_webhooks_flow.py` (append processor tests)

- [ ] **Step 1: Write the failing tests for the processor**

Append to `tests/integration/test_webhooks_flow.py`:

```python
from unittest.mock import AsyncMock, MagicMock

from noviello_funil.brain import Decisao
from noviello_funil.state import (
    Estado, create_lead_if_absent, get_lead_by_conversation,
)
from noviello_funil.webhooks import build_lead_message_processor


def _payload(conversation_id="C-1", lead_id="L-1", from_lead=True, text="oi"):
    """Build a webhook payload matching what Jurichat sends.

    Schema is provisional — adjust when spec §15.6 is validated against
    a real webhook capture. Currently assumes:
      - 'conversation_id' identifies the conversation
      - 'lead_id' identifies the CRM lead
      - 'from_me' is True when the atendente (Mario) sent the message
    """
    return {
        "event": "chat.conversation.updated",
        "id": f"evt-{lead_id}-{text[:3]}",
        "conversation_id": conversation_id,
        "lead_id": lead_id,
        "contact": {"phone": "5511999999999", "name": "Maria"},
        "message": {"text": text, "from_me": not from_lead},
    }


@pytest.mark.asyncio
async def test_processor_creates_lead_and_responds(db_conn):
    fake_jurichat = MagicMock()
    fake_jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "Lead: oi", "summary": "",
    })
    fake_jurichat.send_message = AsyncMock(return_value={"id": "msg-out"})

    async def fake_triagem(**kwargs):
        return Decisao(acao="responder", mensagem="Olá Maria, como posso ajudar?")

    processor = build_lead_message_processor(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        mario_conversation_id="C-MARIO",
        triagem_fn=fake_triagem,
        max_turnos=20,
        followup_horas=48,
    )

    await processor(_payload(text="oi"))

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead is not None
    assert lead["estado"] == Estado.EM_CONVERSA
    assert lead["turnos"] == 1
    fake_jurichat.send_message.assert_awaited_once()
    args, kwargs = fake_jurichat.send_message.call_args
    # send_message(conversation_id, text)
    assert (args[0] if args else kwargs.get("conversation_id")) == "C-1"


@pytest.mark.asyncio
async def test_processor_propor_transitions_to_aguardando_humano(db_conn):
    fake_jurichat = MagicMock()
    fake_jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "Lead: quanto custa?", "summary": "",
    })
    fake_jurichat.send_message = AsyncMock(return_value={"id": "x"})

    async def fake_triagem(**kwargs):
        return Decisao(
            acao="propor",
            mensagem="Nossa proposta é...",
            resumo_caso="Plano negou bariátrica",
        )

    processor = build_lead_message_processor(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        mario_conversation_id="C-MARIO",
        triagem_fn=fake_triagem,
        max_turnos=20,
        followup_horas=48,
    )

    await processor(_payload(text="quanto custa?"))

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    # Two sends: one to lead, one notification to Mario
    assert fake_jurichat.send_message.await_count == 2


@pytest.mark.asyncio
async def test_processor_from_mario_skips_claude(db_conn):
    create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    fake_jurichat = MagicMock()
    fake_jurichat.get_conversation = AsyncMock()
    fake_jurichat.send_message = AsyncMock()

    triagem_calls = {"n": 0}

    async def fake_triagem(**kwargs):
        triagem_calls["n"] += 1
        return Decisao(acao="responder", mensagem="x")

    processor = build_lead_message_processor(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        mario_conversation_id="C-MARIO",
        triagem_fn=fake_triagem,
        max_turnos=20,
        followup_horas=48,
    )

    await processor(_payload(from_lead=False, text="vou cuidar daqui"))

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert triagem_calls["n"] == 0
    fake_jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_ignores_lead_msg_when_aguardando_humano(db_conn):
    create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    from noviello_funil.state import transicao
    lead = get_lead_by_conversation(db_conn, "C-1")
    transicao(db_conn, lead["id"], Estado.AGUARDANDO_HUMANO, motivo="setup")

    fake_jurichat = MagicMock()
    fake_jurichat.send_message = AsyncMock()

    async def fake_triagem(**kwargs):
        return Decisao(acao="responder", mensagem="x")

    processor = build_lead_message_processor(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        mario_conversation_id="C-MARIO",
        triagem_fn=fake_triagem,
        max_turnos=20,
        followup_horas=48,
    )

    await processor(_payload(text="oi de novo"))

    fake_jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_max_turnos_triggers_handoff(db_conn):
    create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    lead = get_lead_by_conversation(db_conn, "C-1")
    # Pre-load turnos to 19; one more message tips over to 20
    db_conn.execute("UPDATE leads SET turnos = 19 WHERE id = ?", (lead["id"],))

    fake_jurichat = MagicMock()
    fake_jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "...",
    })
    fake_jurichat.send_message = AsyncMock(return_value={"id": "x"})

    async def fake_triagem(**kwargs):
        return Decisao(acao="responder", mensagem="continuo")

    processor = build_lead_message_processor(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        mario_conversation_id="C-MARIO",
        triagem_fn=fake_triagem,
        max_turnos=20,
        followup_horas=48,
    )

    await processor(_payload(text="msg 20"))

    after = get_lead_by_conversation(db_conn, "C-1")
    assert after["estado"] == Estado.AGUARDANDO_HUMANO
    # Notification to Mario, but NO reply sent to lead at turn cap
    # (one send_message call for the Mario notification)
    assert fake_jurichat.send_message.await_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_webhooks_flow.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_lead_message_processor'`

- [ ] **Step 3: Append processor factory to webhooks.py**

Append to `src/noviello_funil/webhooks.py`:

```python
from noviello_funil.brain import Decisao, DecisaoInvalida
from noviello_funil.outbound import (
    JurichatClient,
    format_notification,
    notify_mario,
)
from noviello_funil.state import (
    ESTADOS_ATIVOS_CLAUDE,
    Estado,
    bump_turnos,
    create_lead_if_absent,
    get_lead_by_conversation,
    record_lead_message_received,
    register_error,
    transicao,
)


def _is_from_lead(payload: dict[str, Any]) -> bool:
    """Detect if the message in the payload came FROM the lead (not Mario).

    Currently assumes Jurichat sets `message.from_me = True` for outbound
    (atendente-sent) messages. Adjust here if the real payload differs
    (see spec §15.6).
    """
    msg = payload.get("message") or {}
    return not msg.get("from_me", False)


def _extract_text(payload: dict[str, Any]) -> str:
    return (payload.get("message") or {}).get("text", "")


def build_lead_message_processor(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
    mario_conversation_id: str,
    triagem_fn: Callable[..., Any],
    max_turnos: int,
    followup_horas: int,
) -> Callable[[dict[str, Any]], Any]:
    """Build the async processor that handles a single webhook payload.

    Injected dependencies make this testable without real Anthropic/Jurichat.
    `triagem_fn` is the (awaitable) Claude triage callable.
    """

    async def process(payload: dict[str, Any]) -> None:
        conn = get_db()
        conversation_id = payload.get("conversation_id")
        lead_id_external = payload.get("lead_id")
        contact = payload.get("contact") or {}
        ultima_msg = _extract_text(payload)

        if not conversation_id or not lead_id_external:
            # LGPD: log only the keys, never the payload values (may contain
            # lead message body).
            logger.warning("payload missing ids: keys=%s", list(payload.keys()))
            return

        # Branch 1: message from Mario → halt Claude permanently for this lead
        if not _is_from_lead(payload):
            lead = get_lead_by_conversation(conn, conversation_id)
            if lead is None:
                lead = create_lead_if_absent(
                    conn, lead_id_external, conversation_id,
                    contact.get("phone", ""), contact.get("name"),
                )
            if lead["estado"] != Estado.AGUARDANDO_HUMANO:
                transicao(
                    conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                    motivo="mensagem_mario",
                )
            return

        # Branch 2: message from lead
        lead = create_lead_if_absent(
            conn, lead_id_external, conversation_id,
            contact.get("phone", ""), contact.get("name"),
        )
        estado_atual = lead["estado"]

        if estado_atual == Estado.AGUARDANDO_HUMANO:
            # Claude is silent on this lead
            return

        if estado_atual not in ESTADOS_ATIVOS_CLAUDE:
            logger.warning(
                "lead %s in unexpected state %s; skipping",
                lead["id"], estado_atual,
            )
            return

        # Reopen from encerrado_sem_resposta if applicable
        if estado_atual == Estado.ENCERRADO_SEM_RESPOSTA:
            transicao(
                conn, lead["id"], Estado.EM_CONVERSA, motivo="lead_retornou",
            )
            record_lead_message_received(
                conn, lead["id"],
                proxima_acao_horas=followup_horas,
                reset_turnos=True,
            )
        else:
            record_lead_message_received(
                conn, lead["id"], proxima_acao_horas=followup_horas,
            )

        bump_turnos(conn, lead["id"])
        lead = get_lead_by_conversation(conn, conversation_id)
        assert lead is not None

        # Turn cap → force handoff before calling Claude (saves a token)
        if lead["turnos"] >= max_turnos:
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                motivo="max_turnos",
            )
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=format_notification(
                    tipo="turnos",
                    nome=lead["contato_nome"],
                    telefone=lead["contato_telefone"],
                    ultima_msg=ultima_msg,
                    conversation_id=conversation_id,
                ),
            )
            return

        # Pull transcript and call Claude
        try:
            conv = await jurichat.get_conversation(conversation_id)
            transcript = conv.get("transcription", "")
        except Exception as exc:
            register_error(conn, lead["id"], "jurichat_get_conversation_failed")
            logger.exception("get_conversation failed: %s", exc)
            return

        try:
            decisao: Decisao = await triagem_fn(
                conversation_transcript=transcript,
            )
        except DecisaoInvalida:
            register_error(conn, lead["id"], "claude_invalid_json")
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=(
                    f"⚠️ Lead {lead['contato_nome']} ({lead['contato_telefone']}) "
                    f"— Claude retornou JSON inválido. Verifique."
                ),
            )
            return

        # Route by acao
        if decisao.acao == "responder":
            await jurichat.send_message(conversation_id, decisao.mensagem)
            return

        if decisao.acao == "propor":
            await jurichat.send_message(conversation_id, decisao.mensagem)
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                motivo="claude_propor",
                payload={"resumo_caso": decisao.resumo_caso},
            )
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=format_notification(
                    tipo="fechar",
                    nome=lead["contato_nome"],
                    telefone=lead["contato_telefone"],
                    ultima_msg=ultima_msg,
                    resumo=decisao.resumo_caso,
                    conversation_id=conversation_id,
                ),
            )
            return

        if decisao.acao == "handoff":
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                motivo="claude_handoff",
                payload={"motivo_handoff": decisao.motivo_handoff},
            )
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=format_notification(
                    tipo="handoff",
                    nome=lead["contato_nome"],
                    telefone=lead["contato_telefone"],
                    ultima_msg=ultima_msg,
                    motivo=decisao.motivo_handoff,
                    conversation_id=conversation_id,
                ),
            )

    return process
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_webhooks_flow.py -v`
Expected: PASS — all 9 tests green (4 from Task 11 + 5 new)

- [ ] **Step 5: Commit**

```powershell
git add src/noviello_funil/webhooks.py tests/integration/test_webhooks_flow.py
git commit -m "feat(webhooks): cenario A processor — lead msg flow with claude routing"
```

---

### Task 13: Scheduler (Cenário B — follow-ups)

**Files:**
- Create: `src/noviello_funil/scheduler.py`
- Create: `tests/integration/test_scheduler_flow.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_scheduler_flow.py`:

```python
"""Integration tests for the follow-up scheduler."""

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.scheduler import (
    is_eligible_for_followup,
    run_followup_cycle,
)
from noviello_funil.state import (
    Estado, create_lead_if_absent, get_lead_by_conversation,
)


def _make_due_lead(conn, jurichat_lead_id, conversation_id, estado):
    past = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO leads
           (jurichat_lead_id, jurichat_conversation_id, contato_telefone,
            contato_nome, estado, proxima_acao_em)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (jurichat_lead_id, conversation_id, "5511...", "Test", estado, past),
    )


# --- Eligibility rule -----------------------------------------------------

def test_eligible_when_no_tags():
    assert is_eligible_for_followup([]) is True


def test_eligible_when_fazer_followup_present():
    assert is_eligible_for_followup(["Fazer Follow up"]) is True


def test_eligible_when_proposta_enviada_present():
    assert is_eligible_for_followup(["Proposta enviada"]) is True


def test_eligible_when_optin_combined_with_exclusion():
    # opt-in wins
    assert is_eligible_for_followup(["Pagamento pendente", "Fazer Follow up"]) is True


def test_not_eligible_with_only_exclusion_tags():
    assert is_eligible_for_followup(["Cliente Ativo"]) is False
    assert is_eligible_for_followup(["Pagamento pendente", "Reunião marcada"]) is False
    assert is_eligible_for_followup(["Desqualificado"]) is False


# --- Full cycle -----------------------------------------------------------

@pytest.mark.asyncio
async def test_cycle_sends_first_followup_when_in_em_conversa(db_conn):
    _make_due_lead(db_conn, "L-1", "C-1", Estado.EM_CONVERSA)

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(return_value=[])
    fake_jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "Lead: oi (há 2 dias)",
    })
    fake_jurichat.send_message = AsyncMock(return_value={"id": "x"})

    async def fake_followup_gen(**kwargs):
        return "Oi! Conseguiu olhar aquilo que falamos?"

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        gerar_followup_msg=fake_followup_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.FOLLOW_UP_1_ENVIADO
    fake_jurichat.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_cycle_sends_second_followup_when_in_follow_up_1(db_conn):
    _make_due_lead(db_conn, "L-1", "C-1", Estado.FOLLOW_UP_1_ENVIADO)

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(return_value=[])
    fake_jurichat.send_message = AsyncMock(return_value={"id": "x"})

    async def fake_followup_gen(**kwargs):
        raise AssertionError("should not call brain on followup_2")

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        gerar_followup_msg=fake_followup_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.FOLLOW_UP_2_ENVIADO
    sent_text = fake_jurichat.send_message.call_args[0][1]
    assert "encerrar" in sent_text.lower()


@pytest.mark.asyncio
async def test_cycle_closes_silently_when_in_follow_up_2(db_conn):
    _make_due_lead(db_conn, "L-1", "C-1", Estado.FOLLOW_UP_2_ENVIADO)

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(return_value=[])
    fake_jurichat.send_message = AsyncMock()

    async def fake_followup_gen(**kwargs):
        return "x"

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        gerar_followup_msg=fake_followup_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.ENCERRADO_SEM_RESPOSTA
    fake_jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cycle_skips_lead_with_excluding_tag(db_conn):
    _make_due_lead(db_conn, "L-1", "C-1", Estado.EM_CONVERSA)

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(return_value=["Cliente Ativo"])
    fake_jurichat.send_message = AsyncMock()

    async def fake_followup_gen(**kwargs):
        return "x"

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        gerar_followup_msg=fake_followup_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA  # unchanged
    assert lead["erro_atual"] == "excluido_followup_etiqueta"
    assert lead["proxima_acao_em"] is None
    fake_jurichat.send_message.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_scheduler_flow.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'noviello_funil.scheduler'`

- [ ] **Step 3: Implement scheduler module**

Create `src/noviello_funil/scheduler.py`:

```python
"""Follow-up scheduler — invoked hourly by systemd timer.

Reads leads with proxima_acao_em < now and dispatches the right
follow-up step based on their state machine position.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from noviello_funil.outbound import JurichatClient
from noviello_funil.state import (
    Estado,
    clear_next_action,
    list_leads_vencidos,
    register_error,
    schedule_next_action,
    transicao,
)

logger = logging.getLogger(__name__)

OPT_IN_TAGS = frozenset({"Fazer Follow up", "Proposta enviada"})

FOLLOWUP_2_TEXT = (
    "{nome}, percebi que talvez não seja o momento certo. "
    "Posso encerrar nosso atendimento por aqui? "
    "Se preferir continuar depois, é só me chamar novamente."
)


def is_eligible_for_followup(tags: list[str]) -> bool:
    """Strictly opt-in OR no-tag rule per spec §7.2.b."""
    if not tags:
        return True
    tag_set = set(tags)
    return bool(tag_set & OPT_IN_TAGS)


async def run_followup_cycle(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
    gerar_followup_msg: Callable[..., Awaitable[str]],
    followup_2_apos_horas: int,
    encerramento_apos_horas: int,
) -> None:
    """Process all due leads in a single pass."""
    conn = get_db()
    vencidos = list_leads_vencidos(conn)
    logger.info("scheduler tick: %d leads vencidos", len(vencidos))

    for lead in vencidos:
        try:
            tags = await jurichat.get_lead_tags(lead["jurichat_lead_id"])
        except Exception as exc:
            logger.exception("get_lead_tags failed for %s: %s", lead["id"], exc)
            register_error(conn, lead["id"], "jurichat_get_tags_failed")
            continue

        if not is_eligible_for_followup(tags):
            register_error(conn, lead["id"], "excluido_followup_etiqueta")
            clear_next_action(conn, lead["id"])
            continue

        estado = lead["estado"]
        try:
            if estado == Estado.EM_CONVERSA:
                conv = await jurichat.get_conversation(
                    lead["jurichat_conversation_id"]
                )
                texto = await gerar_followup_msg(
                    conversation_transcript=conv.get("transcription", ""),
                )
                await jurichat.send_message(
                    lead["jurichat_conversation_id"], texto,
                )
                transicao(
                    conn, lead["id"], Estado.FOLLOW_UP_1_ENVIADO,
                    motivo="scheduler_followup_1",
                )
                schedule_next_action(
                    conn, lead["id"], horas=followup_2_apos_horas,
                )

            elif estado == Estado.FOLLOW_UP_1_ENVIADO:
                nome = lead["contato_nome"] or "Olá"
                texto = FOLLOWUP_2_TEXT.format(nome=nome)
                await jurichat.send_message(
                    lead["jurichat_conversation_id"], texto,
                )
                transicao(
                    conn, lead["id"], Estado.FOLLOW_UP_2_ENVIADO,
                    motivo="scheduler_followup_2",
                )
                schedule_next_action(
                    conn, lead["id"], horas=encerramento_apos_horas,
                )

            elif estado == Estado.FOLLOW_UP_2_ENVIADO:
                # Silent close — no new message
                transicao(
                    conn, lead["id"], Estado.ENCERRADO_SEM_RESPOSTA,
                    motivo="scheduler_encerramento",
                )
                clear_next_action(conn, lead["id"])

            else:
                logger.warning(
                    "lead %s in unexpected scheduler state %s", lead["id"], estado,
                )
        except Exception as exc:
            logger.exception(
                "scheduler step failed for lead %s: %s", lead["id"], exc,
            )
            register_error(conn, lead["id"], "scheduler_step_failed")


def main() -> int:
    """Entry point for `noviello-followup` console script.

    Reads settings, opens DB + client, runs one cycle, exits 0/1.
    """
    import logging
    from functools import partial

    from anthropic import AsyncAnthropic

    from noviello_funil.brain import gerar_followup_msg as gen, load_skill
    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    conn = connect(settings.database_path)
    run_migrations(conn)

    jurichat = JurichatClient(
        api_key=settings.jurichat_api_key,
        base_url=settings.jurichat_base_url,
    )
    anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    skill = load_skill("saude_suplementar")

    bound_gen = partial(
        gen,
        client=anthropic_client,
        model=settings.anthropic_model,
        skill_content=skill,
    )

    try:
        asyncio.run(run_followup_cycle(
            get_db=lambda: conn,
            jurichat=jurichat,
            gerar_followup_msg=bound_gen,
            followup_2_apos_horas=settings.followup_2_apos_horas,
            encerramento_apos_horas=settings.encerramento_apos_horas,
        ))
        return 0
    except Exception:
        logger.exception("scheduler cycle failed")
        return 1
    finally:
        asyncio.run(jurichat.aclose())
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_scheduler_flow.py -v`
Expected: PASS — 9 tests green (5 eligibility + 4 cycle)

- [ ] **Step 5: Commit**

```powershell
git add src/noviello_funil/scheduler.py tests/integration/test_scheduler_flow.py
git commit -m "feat(scheduler): hourly follow-up cycle with tag eligibility rule"
```

---

### Task 14: FastAPI main entry + wiring

**Files:**
- Create: `src/noviello_funil/main.py`
- Create: `tests/integration/test_main_app.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_main_app.py`:

```python
"""Test that the wired-up app exposes a health check and registers webhooks."""

import pytest
from fastapi.testclient import TestClient


def test_app_starts_and_serves_health(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("JURICHAT_API_KEY", "jk-test")
    monkeypatch.setenv("JURICHAT_WEBHOOK_SECRET", "wh-test")
    monkeypatch.setenv("NOTIFICACAO_TELEFONE", "5511999999999")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")

    # Use a special conversation id for Mario — env var
    monkeypatch.setenv("MARIO_CONVERSATION_ID", "C-MARIO")

    from noviello_funil.main import create_app

    app = create_app()
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_main_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'noviello_funil.main'`

- [ ] **Step 3: Add `mario_conversation_id` to config**

Modify `src/noviello_funil/config.py` — add this field to the `Settings` class:

```python
    # Mario's conversation ID inside Jurichat (the conversation the bot
    # sends Mario's notifications to).
    mario_conversation_id: str
```

- [ ] **Step 4: Update `.env.example`**

Modify `.env.example` — add this line in the Notifications section:

```
# ID da conversa no Jurichat onde o bot manda notificações pro Mario
MARIO_CONVERSATION_ID=
```

- [ ] **Step 5: Implement main.py**

Create `src/noviello_funil/main.py`:

```python
"""FastAPI app factory + wiring.

Composition root: this is the ONE place that imports every other module
and connects them. Everywhere else uses dependency injection.
"""

import logging
from contextlib import asynccontextmanager
from functools import partial

from anthropic import AsyncAnthropic
from fastapi import FastAPI

from noviello_funil.brain import load_skill, triagem
from noviello_funil.config import Settings
from noviello_funil.db import connect, run_migrations
from noviello_funil.outbound import JurichatClient
from noviello_funil.webhooks import build_lead_message_processor, register_webhooks


def create_app() -> FastAPI:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)r}',
    )

    conn = connect(settings.database_path)
    run_migrations(conn)

    jurichat = JurichatClient(
        api_key=settings.jurichat_api_key,
        base_url=settings.jurichat_base_url,
    )
    anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    skill = load_skill("saude_suplementar")

    bound_triagem = partial(
        triagem,
        client=anthropic_client,
        model=settings.anthropic_model,
        skill_content=skill,
    )

    processor = build_lead_message_processor(
        get_db=lambda: conn,
        jurichat=jurichat,
        mario_conversation_id=settings.mario_conversation_id,
        triagem_fn=bound_triagem,
        max_turnos=settings.max_turnos_por_lead,
        followup_horas=settings.followup_1_apos_horas,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await jurichat.aclose()
        conn.close()

    app = FastAPI(title="Noviello Funil Saúde", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"ok": True}

    register_webhooks(
        app,
        get_db=lambda: conn,
        webhook_secret=settings.jurichat_webhook_secret,
        process_lead_message=processor,
    )

    return app


app = create_app() if __name__ != "__main__" else None
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_main_app.py -v`
Expected: PASS — health endpoint works

- [ ] **Step 7: Run the full test suite**

Run: `uv run pytest -v`
Expected: ALL tests pass across all files

- [ ] **Step 8: Commit**

```powershell
git add src/noviello_funil/main.py src/noviello_funil/config.py .env.example tests/integration/test_main_app.py
git commit -m "feat(main): fastapi app factory + dependency wiring"
```

---

### Task 15: Smoke test script

**Files:**
- Create: `scripts/smoke.sh`
- Create: `scripts/smoke_send_webhook.py`

- [ ] **Step 1: Create the Python helper that signs and posts a fake webhook**

Create `scripts/smoke_send_webhook.py`:

```python
"""Send a fake Jurichat webhook to the local server.

Usage:
  uv run python scripts/smoke_send_webhook.py [--text "msg do lead"]

Reads JURICHAT_WEBHOOK_SECRET from .env, signs the payload with HMAC-SHA256,
posts to http://127.0.0.1:8000/webhooks/jurichat.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="Olá, vi que vocês trabalham com plano de saúde?")
    parser.add_argument("--conversation-id", default="C-SMOKE-1")
    parser.add_argument("--lead-id", default="L-SMOKE-1")
    parser.add_argument("--from-me", action="store_true", help="Simulate Mario sending")
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhooks/jurichat")
    args = parser.parse_args()

    secret = os.environ.get("JURICHAT_WEBHOOK_SECRET")
    if not secret:
        # Try loading from .env
        env_path = ".env"
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("JURICHAT_WEBHOOK_SECRET="):
                        secret = line.split("=", 1)[1].strip()
                        break
    if not secret:
        print("ERROR: JURICHAT_WEBHOOK_SECRET not set", file=sys.stderr)
        return 1

    payload = {
        "event": "chat.conversation.updated",
        "id": f"evt-smoke-{os.urandom(4).hex()}",
        "conversation_id": args.conversation_id,
        "lead_id": args.lead_id,
        "contact": {"phone": "5511988887777", "name": "Lead Smoke"},
        "message": {"text": args.text, "from_me": args.from_me},
    }

    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    resp = httpx.post(
        args.url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-JuriChat-Signature": sig,
        },
        timeout=10.0,
    )
    print(f"status={resp.status_code} body={resp.text}")
    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Create the shell driver (runs on Linux/macOS VPS)**

Create `scripts/smoke.sh`:

```bash
#!/usr/bin/env bash
# Smoke test: send a fake webhook and verify the server processes it.
#
# Pre-req: the service must be running locally (uvicorn or systemd).
# Run:   bash scripts/smoke.sh

set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== smoke 1: lead inicial ==="
uv run python scripts/smoke_send_webhook.py \
    --conversation-id "C-SMOKE-$(date +%s)" \
    --lead-id "L-SMOKE-$(date +%s)" \
    --text "Olá, plano negou minha cirurgia"

echo
echo "Now check:"
echo "  - The server log (journalctl -u noviello-funil -f) for processing"
echo "  - The local sqlite DB:"
echo "      sqlite3 data/noviello.db 'SELECT id, estado, turnos FROM leads;'"
```

- [ ] **Step 3: Commit**

```powershell
git add scripts/
git commit -m "feat(scripts): smoke test sender + driver"
```

---

### Task 16: Deploy artifacts (systemd + nginx)

**Files:**
- Create: `deploy/noviello-funil.service`
- Create: `deploy/noviello-followup.service`
- Create: `deploy/noviello-followup.timer`
- Create: `deploy/nginx.conf`

- [ ] **Step 1: Create the FastAPI systemd service unit**

Create `deploy/noviello-funil.service`:

```ini
[Unit]
Description=Noviello Funil Saude - FastAPI
After=network.target
Requires=network.target

[Service]
Type=simple
User=noviello
Group=noviello
WorkingDirectory=/opt/noviello-funil-saude
Environment=PATH=/opt/noviello-funil-saude/.venv/bin
EnvironmentFile=/opt/noviello-funil-saude/.env
ExecStart=/opt/noviello-funil-saude/.venv/bin/uvicorn \
    noviello_funil.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --no-access-log
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create the follow-up oneshot service unit**

Create `deploy/noviello-followup.service`:

```ini
[Unit]
Description=Noviello Funil Saude - Follow-up Cycle
After=network.target

[Service]
Type=oneshot
User=noviello
Group=noviello
WorkingDirectory=/opt/noviello-funil-saude
EnvironmentFile=/opt/noviello-funil-saude/.env
ExecStart=/opt/noviello-funil-saude/.venv/bin/noviello-followup
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
```

- [ ] **Step 3: Create the timer that triggers the follow-up service hourly**

Create `deploy/noviello-followup.timer`:

```ini
[Unit]
Description=Run Noviello follow-up cycle hourly

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
AccuracySec=1min
Persistent=true
Unit=noviello-followup.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Create the nginx reverse proxy config**

Create `deploy/nginx.conf`:

```nginx
# /etc/nginx/sites-available/noviello-funil
# Symlink into sites-enabled and reload nginx.
#
# IMPORTANT: replace the server_name with the actual subdomain you'll use
# (e.g., funil.noviello.adv.br). Run certbot AFTER nginx is reloaded with
# the http-only config; certbot will mutate this file to add TLS.

server {
    listen 80;
    server_name funil.noviello.adv.br;

    # Limit body size to prevent malicious payloads
    client_max_body_size 64K;

    # Proxy only the webhook path
    location /webhooks/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        proxy_connect_timeout 5s;
    }

    # Health check (optional — useful for uptime monitoring)
    location /health {
        proxy_pass http://127.0.0.1:8000;
        access_log off;
    }

    # Block everything else
    location / {
        return 404;
    }
}
```

- [ ] **Step 5: Commit**

```powershell
git add deploy/
git commit -m "feat(deploy): systemd units + nginx config for vps"
```

---

### Task 17: README with setup, dev and deploy instructions

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README**

Create `README.md`:

```markdown
# Noviello Funil Saúde

Atendente IA para leads de plano de saúde no WhatsApp via Jurichat e Claude.

**Spec:** [`docs/superpowers/specs/2026-06-03-noviello-funil-saude-design.md`](docs/superpowers/specs/2026-06-03-noviello-funil-saude-design.md)
**Plano:** [`docs/superpowers/plans/2026-06-03-noviello-funil-saude.md`](docs/superpowers/plans/2026-06-03-noviello-funil-saude.md)

## Stack

Python 3.11 · FastAPI · SQLite · `httpx` · `anthropic` SDK · `pydantic-settings` · `pytest` · `uv`

## Dev — Windows / macOS / Linux

```bash
# 1. Install uv if needed: https://docs.astral.sh/uv/getting-started/installation/
# 2. Sync deps:
uv sync --all-extras

# 3. Copy env template and fill in secrets:
cp .env.example .env
# Edit .env with real values from C:\Users\mario\.secrets\noviello-automacao.env

# 4. Run tests:
uv run pytest -v

# 5. Run the server locally:
uv run uvicorn noviello_funil.main:app --reload --port 8000

# 6. Smoke test (in another shell):
uv run python scripts/smoke_send_webhook.py --text "oi, vocês trabalham com plano de saúde?"
```

## Deploy — VPS Hostinger Ubuntu 22.04

Prereqs on the VPS:
- Python 3.11 (or install via deadsnakes)
- `uv` installed system-wide (or for the `noviello` user)
- nginx, certbot
- A subdomain pointing to the VPS IP (e.g., `funil.noviello.adv.br`)

```bash
# As root (one-time setup)
useradd --system --create-home --shell /bin/bash noviello
mkdir -p /opt/noviello-funil-saude
chown noviello:noviello /opt/noviello-funil-saude

# As noviello user
sudo -iu noviello
cd /opt/noviello-funil-saude
git clone <your-repo-url> .
uv sync --no-dev
cp .env.example .env
# Edit .env with production values
mkdir -p data

# Back as root: install systemd units
cp deploy/noviello-funil.service /etc/systemd/system/
cp deploy/noviello-followup.service /etc/systemd/system/
cp deploy/noviello-followup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now noviello-funil.service
systemctl enable --now noviello-followup.timer

# Install nginx config
cp deploy/nginx.conf /etc/nginx/sites-available/noviello-funil
ln -s /etc/nginx/sites-available/noviello-funil /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Get TLS cert
certbot --nginx -d funil.noviello.adv.br

# Verify
curl https://funil.noviello.adv.br/health   # should return {"ok":true}
systemctl status noviello-funil.service
systemctl list-timers noviello-followup.timer
```

## Operations

```bash
# Tail the live log
journalctl -u noviello-funil.service -f

# Inspect the DB
sqlite3 /opt/noviello-funil-saude/data/noviello.db
sqlite> .mode column
sqlite> .headers on
sqlite> SELECT id, contato_nome, estado, turnos, erro_atual FROM leads;
sqlite> SELECT * FROM transicoes ORDER BY criado_em DESC LIMIT 20;

# Force a follow-up cycle right now (instead of waiting for the timer)
systemctl start noviello-followup.service

# Reload after code change
git pull
uv sync --no-dev
systemctl restart noviello-funil.service
```

## Pre-production checklist (per spec §15)

- [ ] Validate that Jurichat fires webhook per message (not only per CRM stage change)
- [ ] Map exact webhook payload shape and confirm `message.from_me` semantics
- [ ] Confirm Jurichat endpoint for listing lead tags
- [ ] Rotate Jurichat API key (the previous one was exposed in chat — see spec §15.5)
- [ ] Pick the actual subdomain for the webhook (currently `funil.noviello.adv.br` is a placeholder)
- [ ] Define `MARIO_CONVERSATION_ID` — the Jurichat conversation that receives notifications
- [ ] Establish daily backup of `data/noviello.db` (rsync to another disk or off-VPS)
```

- [ ] **Step 2: Commit**

```powershell
git add README.md
git commit -m "docs: README with setup, dev, deploy, and ops instructions"
```

---

## Definition of Done

The MVP is shippable when:

1. `uv run pytest -v` — ALL tests pass (45+ across unit + integration)
2. `uv run python scripts/smoke_send_webhook.py` — round-trips OK against `uv run uvicorn` locally with mocked Anthropic (set `ANTHROPIC_API_KEY` to a real key and observe a real call, or stub `triagem` in main.py for offline smoke)
3. Project deploys cleanly per README on a fresh VPS
4. All 6 items in `Pre-production checklist` resolved
5. First real lead conversation completes end-to-end:
   `webhook received → Claude responded → notification reached Mario`

## What's NOT in this plan (deferred per spec §16)

- ZapSign integration (contracts)
- Asaas/Juridiq direct API (covered by native Jurichat integration)
- Move-to-Ganho automation
- Human approval via WhatsApp command (`APROVAR <id>`)
- Web dashboard
- Aéreo / other verticals
