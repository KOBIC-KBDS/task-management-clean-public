from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from .domain import (
    ApprovalDecision,
    ApprovalRequest,
    IncomingMessage,
    Proposal,
)


class TeamTaskStore:
    """SQLite state store with a JSONL audit log."""

    def __init__(self, db_path: str | Path, event_log_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.event_log_path = Path(event_log_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def has_message(self, message_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("select 1 from messages where message_id = ?", (message_id,)).fetchone()
        return row is not None

    def record_message(self, message: IncomingMessage) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into messages(message_id, sender_id, chat_id, visibility, text, received_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.sender_id,
                    message.chat_id,
                    message.visibility,
                    message.text,
                    _dt(message.received_at),
                ),
            )

    def delete_message(self, message_id: str) -> None:
        with self._connect() as conn:
            conn.execute("delete from messages where message_id = ?", (message_id,))

    def save_proposal(self, proposal: Proposal) -> None:
        with self._connect() as conn:
            _upsert_proposal(conn, proposal)

    def save_proposals_atomic(self, proposals: Iterable[Proposal]) -> None:
        """Persist a related proposal mutation in one SQLite transaction."""

        proposal_batch = tuple(proposals)
        if not proposal_batch:
            return
        with self._connect() as conn:
            for proposal in proposal_batch:
                _upsert_proposal(conn, proposal)

    def save_proposals_with_audit_atomic(
        self,
        proposals: Iterable[Proposal],
        events: Iterable[tuple[str, dict[str, Any], datetime]],
    ) -> None:
        """Persist proposal mutations and their recoverable audit outbox together."""

        proposal_batch = tuple(proposals)
        event_batch = tuple(events)
        if not proposal_batch and not event_batch:
            return
        with self._connect() as conn:
            for proposal in proposal_batch:
                _upsert_proposal(conn, proposal)
            for event_type, payload, occurred_at in event_batch:
                conn.execute(
                    """
                    insert into audit_event_outbox(event_type, occurred_at, payload_json, delivered_at)
                    values (?, ?, ?, null)
                    """,
                    (
                        event_type,
                        _dt(occurred_at),
                        json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True),
                    ),
                )
        try:
            self.flush_audit_outbox()
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            # Proposal state and its audit rows are already durable together.
            # JSONL is a projection; a later append/read/maintenance cycle retries it.
            pass

    def save_proposals_and_approval_with_audit_atomic(
        self,
        proposals: Iterable[Proposal],
        *,
        approval_request: ApprovalRequest | None,
        approval_decision: ApprovalDecision | None,
        events: Iterable[tuple[str, dict[str, Any], datetime]],
    ) -> None:
        """Persist a structural proposal change and its approval resolution together."""

        proposal_batch = tuple(proposals)
        event_batch = tuple(events)
        if (approval_request is None) != (approval_decision is None):
            raise ValueError("approval_request_and_decision_must_be_paired")
        with self._connect() as conn:
            for proposal in proposal_batch:
                _upsert_proposal(conn, proposal)
            if approval_request is not None and approval_decision is not None:
                _upsert_approval_request(conn, approval_request)
                _upsert_approval_decision(conn, approval_decision)
            for event_type, payload, occurred_at in event_batch:
                conn.execute(
                    """
                    insert into audit_event_outbox(event_type, occurred_at, payload_json, delivered_at)
                    values (?, ?, ?, null)
                    """,
                    (
                        event_type,
                        _dt(occurred_at),
                        json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True),
                    ),
                )
        try:
            self.flush_audit_outbox()
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            pass

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        with self._connect() as conn:
            row = conn.execute("select * from proposals where proposal_id = ?", (proposal_id,)).fetchone()
        return _proposal_from_row(row) if row is not None else None

    def list_proposals(self, *, status: str | None = None) -> tuple[Proposal, ...]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute("select * from proposals order by created_at, proposal_id").fetchall()
            else:
                rows = conn.execute(
                    "select * from proposals where status = ? order by created_at, proposal_id",
                    (status,),
                ).fetchall()
        return tuple(_proposal_from_row(row) for row in rows)

    def save_approval_request(self, request: ApprovalRequest) -> None:
        with self._connect() as conn:
            _upsert_approval_request(conn, request)

    def get_approval_request(self, request_id: str) -> ApprovalRequest | None:
        with self._connect() as conn:
            row = conn.execute("select * from approval_requests where request_id = ?", (request_id,)).fetchone()
        return _request_from_row(row) if row is not None else None

    def list_approval_requests(
        self,
        *,
        proposal_id: str | None = None,
        approver_id: str | None = None,
        status: str | None = None,
    ) -> tuple[ApprovalRequest, ...]:
        clauses: list[str] = []
        params: list[str] = []
        if proposal_id is not None:
            clauses.append("proposal_id = ?")
            params.append(proposal_id)
        if approver_id is not None:
            clauses.append("approver_id = ?")
            params.append(approver_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"select * from approval_requests {where} order by requested_at, request_id",
                tuple(params),
            ).fetchall()
        return tuple(_request_from_row(row) for row in rows)

    def save_approval_decision(self, decision: ApprovalDecision) -> None:
        with self._connect() as conn:
            _upsert_approval_decision(conn, decision)

    def mark_proposal_applied(self, proposal_id: str, export_item_id: str, *, applied_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert or replace into applied_exports(proposal_id, export_item_id, applied_at) values (?, ?, ?)",
                (proposal_id, export_item_id, _dt(applied_at)),
            )

    def get_applied_export(self, proposal_id: str) -> dict[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select proposal_id, export_item_id, applied_at from applied_exports where proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "proposal_id": str(row["proposal_id"]),
            "export_item_id": str(row["export_item_id"]),
            "applied_at": str(row["applied_at"]),
        }

    def list_applied_exports(self) -> dict[str, dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "select proposal_id, export_item_id, applied_at from applied_exports order by applied_at, proposal_id"
            ).fetchall()
        return {
            str(row["proposal_id"]): {
                "proposal_id": str(row["proposal_id"]),
                "export_item_id": str(row["export_item_id"]),
                "applied_at": str(row["applied_at"]),
            }
            for row in rows
        }

    def get_integration_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("select value from integration_state where key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_integration_state(self, key: str, value: str, *, updated_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into integration_state(key, value, updated_at)
                values (?, ?, ?)
                on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, _dt(updated_at)),
            )

    def has_outbound_delivery(self, dedupe_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("select 1 from outbound_deliveries where dedupe_key = ?", (dedupe_key,)).fetchone()
        return row is not None

    def enqueue_inbound_event(
        self,
        *,
        event_id: str,
        provider: str,
        event_type: str,
        message_id: str,
        payload: dict[str, Any],
        received_at: datetime,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                insert or ignore into inbound_event_queue(
                    event_id, provider, event_type, message_id, payload_json,
                    status, attempts, received_at, updated_at, last_error
                )
                values (?, ?, ?, ?, ?, 'pending', 0, ?, ?, '')
                """,
                (
                    event_id,
                    provider,
                    event_type,
                    message_id,
                    json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True),
                    _dt(received_at),
                    _dt(received_at),
                ),
            )
        return cursor.rowcount > 0

    def list_pending_inbound_events(
        self,
        *,
        provider: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        params: list[Any] = ["pending"]
        where = "status = ?"
        if provider is not None:
            where += " and provider = ?"
            params.append(provider)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select * from inbound_event_queue
                where {where}
                order by received_at, event_id
                limit ?
                """,
                tuple(params),
            ).fetchall()
        return tuple(_inbound_event_from_row(row) for row in rows)

    def mark_inbound_event(
        self,
        event_id: str,
        *,
        status: str,
        updated_at: datetime,
        last_error: str = "",
        increment_attempts: bool = False,
    ) -> None:
        attempts_sql = "attempts = attempts + 1," if increment_attempts else ""
        with self._connect() as conn:
            conn.execute(
                f"""
                update inbound_event_queue
                set status = ?, {attempts_sql} updated_at = ?, last_error = ?
                where event_id = ?
                """,
                (status, _dt(updated_at), last_error[:1000], event_id),
            )

    def enqueue_outbound_message(
        self,
        *,
        dedupe_key: str,
        provider: str,
        surface: str,
        recipient_id: str,
        message_type: str,
        proposal_id: str,
        approval_request_id: str,
        text: str,
        message: dict[str, Any],
        queued_at: datetime,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                insert or ignore into outbound_message_queue(
                    dedupe_key, provider, surface, recipient_id, message_type,
                    proposal_id, approval_request_id, text, message_json,
                    status, attempts, provider_message_id, last_error, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, '', '', ?, ?)
                """,
                (
                    dedupe_key,
                    provider,
                    surface,
                    recipient_id,
                    message_type,
                    proposal_id,
                    approval_request_id,
                    text,
                    json.dumps(_json_safe(message), ensure_ascii=False, sort_keys=True),
                    _dt(queued_at),
                    _dt(queued_at),
                ),
            )
            if cursor.rowcount == 0:
                cursor = conn.execute(
                    """
                    update outbound_message_queue
                    set provider = ?, surface = ?, recipient_id = ?, message_type = ?,
                        proposal_id = ?, approval_request_id = ?, text = ?, message_json = ?,
                        status = 'pending', provider_message_id = '', last_error = '', updated_at = ?
                    where dedupe_key = ? and status in ('failed', 'skipped')
                    """,
                    (
                        provider,
                        surface,
                        recipient_id,
                        message_type,
                        proposal_id,
                        approval_request_id,
                        text,
                        json.dumps(_json_safe(message), ensure_ascii=False, sort_keys=True),
                        _dt(queued_at),
                        dedupe_key,
                    ),
                )
        return cursor.rowcount > 0

    def list_pending_outbound_messages(
        self,
        *,
        provider: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        params: list[Any] = ["pending"]
        where = "status = ?"
        if provider is not None:
            where += " and provider = ?"
            params.append(provider)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select * from outbound_message_queue
                where {where}
                order by created_at, dedupe_key
                limit ?
                """,
                tuple(params),
            ).fetchall()
        return tuple(_outbound_queue_from_row(row) for row in rows)

    def mark_outbound_message(
        self,
        dedupe_key: str,
        *,
        status: str,
        updated_at: datetime,
        provider_message_id: str = "",
        last_error: str = "",
        increment_attempts: bool = False,
    ) -> None:
        attempts_sql = "attempts = attempts + 1," if increment_attempts else ""
        with self._connect() as conn:
            conn.execute(
                f"""
                update outbound_message_queue
                set status = ?, {attempts_sql} provider_message_id = ?, updated_at = ?, last_error = ?
                where dedupe_key = ?
                """,
                (status, provider_message_id, _dt(updated_at), last_error[:1000], dedupe_key),
            )

    def record_outbound_delivery(
        self,
        *,
        dedupe_key: str,
        surface: str,
        recipient_id: str,
        provider: str,
        provider_message_id: str,
        sent_at: datetime,
        payload: dict[str, Any],
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                insert or ignore into outbound_deliveries(
                    dedupe_key, surface, recipient_id, provider, provider_message_id, sent_at, payload_json
                )
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dedupe_key,
                    surface,
                    recipient_id,
                    provider,
                    provider_message_id,
                    _dt(sent_at),
                    json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True),
                ),
            )
        return cursor.rowcount > 0

    def append_event(self, event_type: str, payload: dict[str, Any], *, occurred_at: datetime) -> None:
        self.flush_audit_outbox()
        event = {
            "type": event_type,
            "occurred_at": _dt(occurred_at),
            "payload": _json_safe(payload),
        }
        with self.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def flush_audit_outbox(self) -> int:
        """Append pending transactional audit rows to JSONL and mark them delivered."""

        with self._connect() as conn:
            conn.execute("begin immediate")
            rows = conn.execute(
                """
                select id, event_type, occurred_at, payload_json
                from audit_event_outbox
                where delivered_at is null
                order by id
                """
            ).fetchall()
            if not rows:
                return 0
            delivered_ids = self._event_log_outbox_ids()
            with self.event_log_path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    if row["id"] in delivered_ids:
                        continue
                    event = {
                        "type": row["event_type"],
                        "occurred_at": row["occurred_at"],
                        "payload": json.loads(row["payload_json"]),
                        "outbox_id": row["id"],
                    }
                    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            delivered_at = _dt(datetime.now())
            conn.executemany(
                "update audit_event_outbox set delivered_at = ? where id = ? and delivered_at is null",
                ((delivered_at, row["id"]) for row in rows),
            )
            return len(rows)

    def _event_log_outbox_ids(self) -> set[int]:
        if not self.event_log_path.is_file():
            return set()
        delivered_ids: set[int] = set()
        for line in self.event_log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            outbox_id = event.get("outbox_id")
            if isinstance(outbox_id, int):
                delivered_ids.add(outbox_id)
        return delivered_ids

    def read_events(self) -> tuple[dict[str, Any], ...]:
        self.flush_audit_outbox()
        if not self.event_log_path.exists():
            return ()
        events = []
        for line in self.event_log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return tuple(events)

    def read_recent_events(self, max_events: int = 200, *, tail_bytes: int = 65536) -> tuple[dict[str, Any], ...]:
        """Return only the most recent events without parsing the whole log.

        Seeks ``tail_bytes`` from EOF and parses the trailing lines, returning
        at most ``max_events`` events (oldest-first within that tail).  Intended
        for hot inbound paths that only need a short recent window; for a full
        replay use :meth:`read_events`.
        """

        self.flush_audit_outbox()
        if not self.event_log_path.exists():
            return ()
        file_size = self.event_log_path.stat().st_size
        with self.event_log_path.open("rb") as handle:
            seek_to = max(0, file_size - tail_bytes)
            handle.seek(seek_to)
            chunk = handle.read()
        # Drop a leading partial line when we seeked into the middle of the file.
        if seek_to > 0:
            newline = chunk.find(b"\n")
            chunk = chunk[newline + 1 :] if newline != -1 else b""
        text = chunk.decode("utf-8")
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
        if len(events) > max_events:
            events = events[-max_events:]
        return tuple(events)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists messages (
                    message_id text primary key,
                    sender_id text not null,
                    chat_id text not null,
                    visibility text not null,
                    text text not null,
                    received_at text not null
                );

                create table if not exists proposals (
                    proposal_id text primary key,
                    source_message_id text not null,
                    proposer_id text not null,
                    title text not null,
                    raw_text text not null,
                    kind text not null,
                    status text not null,
                    assigned_to text not null,
                    task_management_area text not null,
                    discussion_id text not null,
                    message_id text not null,
                    required_approvers text not null,
                    approvals text not null,
                    missing_slots text not null,
                    due_date text,
                    scheduled_date text,
                    time_window text not null,
                    source_url text not null,
                    source_export_path text not null,
                    created_at text,
                    updated_at text,
                    metadata text not null
                );

                create table if not exists approval_requests (
                    request_id text primary key,
                    proposal_id text not null,
                    approver_id text not null,
                    status text not null,
                    requested_at text,
                    decided_at text
                );

                create table if not exists approval_decisions (
                    request_id text primary key,
                    proposal_id text not null,
                    approver_id text not null,
                    decision text not null,
                    decided_at text not null
                );

                create table if not exists applied_exports (
                    proposal_id text primary key,
                    export_item_id text not null,
                    applied_at text not null
                );

                create table if not exists integration_state (
                    key text primary key,
                    value text not null,
                    updated_at text not null
                );

                create table if not exists outbound_deliveries (
                    dedupe_key text primary key,
                    surface text not null,
                    recipient_id text not null,
                    provider text not null,
                    provider_message_id text not null,
                    sent_at text not null,
                    payload_json text not null
                );

                create table if not exists inbound_event_queue (
                    event_id text primary key,
                    provider text not null,
                    event_type text not null,
                    message_id text not null,
                    payload_json text not null,
                    status text not null,
                    attempts integer not null,
                    received_at text not null,
                    updated_at text not null,
                    last_error text not null
                );

                create table if not exists outbound_message_queue (
                    dedupe_key text primary key,
                    provider text not null,
                    surface text not null,
                    recipient_id text not null,
                    message_type text not null,
                    proposal_id text not null,
                    approval_request_id text not null,
                    text text not null,
                    message_json text not null,
                    status text not null,
                    attempts integer not null,
                    provider_message_id text not null,
                    last_error text not null,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists audit_event_outbox (
                    id integer primary key autoincrement,
                    event_type text not null,
                    occurred_at text not null,
                    payload_json text not null,
                    delivered_at text
                );
                """
            )
            _migrate_legacy_schema(conn)


def _upsert_proposal(conn: sqlite3.Connection, proposal: Proposal) -> None:
    conn.execute(
        """
        insert into proposals(
            proposal_id, source_message_id, proposer_id, title, raw_text, kind, status,
            assigned_to, task_management_area, discussion_id, message_id, required_approvers,
            approvals, missing_slots, due_date, scheduled_date, time_window, source_url,
            source_export_path, created_at, updated_at, metadata
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(proposal_id) do update set
            title = excluded.title,
            raw_text = excluded.raw_text,
            kind = excluded.kind,
            status = excluded.status,
            assigned_to = excluded.assigned_to,
            task_management_area = excluded.task_management_area,
            required_approvers = excluded.required_approvers,
            approvals = excluded.approvals,
            missing_slots = excluded.missing_slots,
            due_date = excluded.due_date,
            scheduled_date = excluded.scheduled_date,
            time_window = excluded.time_window,
            source_url = excluded.source_url,
            source_export_path = excluded.source_export_path,
            updated_at = excluded.updated_at,
            metadata = excluded.metadata
        """,
        _proposal_row(proposal),
    )


def _upsert_approval_request(conn: sqlite3.Connection, request: ApprovalRequest) -> None:
    conn.execute(
        """
        insert into approval_requests(
            request_id, proposal_id, approver_id, status, requested_at, decided_at
        )
        values (?, ?, ?, ?, ?, ?)
        on conflict(request_id) do update set
            status = excluded.status,
            decided_at = excluded.decided_at
        where approval_requests.status = 'pending'
        """,
        (
            request.request_id,
            request.proposal_id,
            request.approver_id,
            request.status,
            _dt(request.requested_at),
            _dt(request.decided_at),
        ),
    )


def _upsert_approval_decision(conn: sqlite3.Connection, decision: ApprovalDecision) -> None:
    conn.execute(
        """
        insert or replace into approval_decisions(request_id, proposal_id, approver_id, decision, decided_at)
        values (?, ?, ?, ?, ?)
        """,
        (
            decision.request_id,
            decision.proposal_id,
            decision.approver_id,
            decision.decision,
            _dt(decision.decided_at),
        ),
    )


def _proposal_row(proposal: Proposal) -> tuple[Any, ...]:
    return (
        proposal.proposal_id,
        proposal.source_message_id,
        proposal.proposer_id,
        proposal.title,
        proposal.raw_text,
        proposal.kind,
        proposal.status,
        proposal.assigned_to,
        proposal.task_management_area,
        proposal.discussion_id,
        proposal.message_id,
        json.dumps(list(proposal.required_approvers), ensure_ascii=False),
        json.dumps(list(proposal.approvals), ensure_ascii=False),
        json.dumps(list(proposal.missing_slots), ensure_ascii=False),
        _date(proposal.due_date),
        _date(proposal.scheduled_date),
        proposal.time_window,
        proposal.source_url,
        proposal.source_export_path,
        _dt(proposal.created_at),
        _dt(proposal.updated_at),
        json.dumps(proposal.metadata, ensure_ascii=False, sort_keys=True),
    )


def _migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    proposal_columns = {
        row["name"]
        for row in conn.execute("pragma table_info(proposals)").fetchall()
    }
    if "task_management_area" not in proposal_columns and "household_area" in proposal_columns:
        conn.execute("alter table proposals add column task_management_area text not null default 'general'")
        conn.execute(
            """
            update proposals
            set task_management_area = coalesce(nullif(household_area, ''), 'general')
            """
        )


def _proposal_from_row(row: sqlite3.Row) -> Proposal:
    return Proposal(
        proposal_id=row["proposal_id"],
        source_message_id=row["source_message_id"],
        proposer_id=row["proposer_id"],
        title=row["title"],
        raw_text=row["raw_text"],
        kind=row["kind"],
        status=row["status"],
        assigned_to=row["assigned_to"],
        task_management_area=row["task_management_area"],
        discussion_id=row["discussion_id"],
        message_id=row["message_id"],
        required_approvers=tuple(json.loads(row["required_approvers"])),
        approvals=tuple(json.loads(row["approvals"])),
        missing_slots=tuple(json.loads(row["missing_slots"])),
        due_date=_parse_date(row["due_date"]),
        scheduled_date=_parse_date(row["scheduled_date"]),
        time_window=row["time_window"],
        source_url=row["source_url"],
        source_export_path=row["source_export_path"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        metadata=json.loads(row["metadata"]),
    )


def _request_from_row(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=row["request_id"],
        proposal_id=row["proposal_id"],
        approver_id=row["approver_id"],
        status=row["status"],
        requested_at=_parse_dt(row["requested_at"]),
        decided_at=_parse_dt(row["decided_at"]),
    )


def _inbound_event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": str(row["event_id"]),
        "provider": str(row["provider"]),
        "event_type": str(row["event_type"]),
        "message_id": str(row["message_id"]),
        "payload": json.loads(row["payload_json"]),
        "status": str(row["status"]),
        "attempts": int(row["attempts"]),
        "received_at": str(row["received_at"]),
        "updated_at": str(row["updated_at"]),
        "last_error": str(row["last_error"]),
    }


def _outbound_queue_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "dedupe_key": str(row["dedupe_key"]),
        "provider": str(row["provider"]),
        "surface": str(row["surface"]),
        "recipient_id": str(row["recipient_id"]),
        "message_type": str(row["message_type"]),
        "proposal_id": str(row["proposal_id"]),
        "approval_request_id": str(row["approval_request_id"]),
        "text": str(row["text"]),
        "message": json.loads(row["message_json"]),
        "status": str(row["status"]),
        "attempts": int(row["attempts"]),
        "provider_message_id": str(row["provider_message_id"]),
        "last_error": str(row["last_error"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    return value


def _dt(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
