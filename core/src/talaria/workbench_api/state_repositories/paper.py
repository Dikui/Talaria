"""常驻服务的草案修订、确认与出站队列。"""

import sqlite3
from collections.abc import Callable
from uuid import uuid4

from talaria.foundation.errors import NotFoundError, TalariaError
from talaria.foundation.paper import ConfirmedDraft, DraftConfirmation, DraftContent, DraftRecord, DraftReceipt

from ._sqlite import write_transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_drafts (
    draft_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    sub_account_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    content TEXT NOT NULL,
    basis_cutoff_ts INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    confirmation TEXT,
    claimed INTEGER NOT NULL DEFAULT 0,
    receipt TEXT
);
CREATE TABLE IF NOT EXISTS paper_submissions (
    seq INTEGER PRIMARY KEY,
    draft_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    UNIQUE (draft_id, version)
);
INSERT OR IGNORE INTO paper_submissions(draft_id, version)
    SELECT draft_id, version FROM paper_drafts WHERE confirmation IS NOT NULL
    ORDER BY json_extract(confirmation, '$.confirmed_ts'), rowid;
UPDATE paper_drafts SET receipt = NULL
    WHERE json_extract(receipt, '$.reason') IN ('DRAFT_INTERRUPTED', 'DRAFT_DATA_UNAVAILABLE');
CREATE INDEX IF NOT EXISTS paper_draft_inbox
    ON paper_drafts (sub_account_id, instrument_id) WHERE confirmation IS NOT NULL AND receipt IS NULL;
"""


class DraftConflict(TalariaError):
    code = "DRAFT_CONFLICT"


class DraftRepository:
    def __init__(self, db: sqlite3.Connection, now_ms: Callable[[], int]) -> None:
        self._db = db
        self._now = now_ms

    def _record(self, row) -> DraftRecord:
        draft_id, version, payload, confirmed, claimed, receipt = row
        content = DraftContent.model_validate_json(payload)
        return DraftRecord(
            draft_id=draft_id, version=version, content_hash=content.digest(), content=content,
            status="CONFIRMED" if confirmed else "STAGED" if version == 1 else "MODIFIED",
            claimed=bool(claimed), receipt=DraftReceipt.model_validate_json(receipt) if receipt else None,
        )

    def list(self) -> list[DraftRecord]:
        return [self._record(row) for row in self._db.execute(
            "SELECT draft_id, version, content, confirmation, claimed, receipt FROM paper_drafts ORDER BY rowid DESC",
        )]

    def get(self, draft_id: str) -> DraftRecord:
        row = self._db.execute(
            "SELECT draft_id, version, content, confirmation, claimed, receipt FROM paper_drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("找不到交易草案")
        return self._record(row)

    def save(self, content: DraftContent, basis: int) -> DraftRecord:
        draft_id = uuid4().hex
        self._db.execute(
            """INSERT INTO paper_drafts
               (draft_id, version, sub_account_id, instrument_id, content, basis_cutoff_ts, created_ts)
               VALUES (?, 1, ?, ?, ?, ?, ?)""",
            (draft_id, content.sub_account_id, content.instrument_id, content.model_dump_json(), basis, self._now()),
        )
        return self.get(draft_id)

    def edit(self, draft_id: str, version: int, content: DraftContent, basis: int) -> DraftRecord:
        record = self.get(draft_id)
        if record.version != version or record.claimed:
            raise DraftConflict("草案已更新或已送达账户，请重新读取")
        self._db.execute(
            """UPDATE paper_drafts SET version = version + 1, sub_account_id = ?, instrument_id = ?,
               content = ?, basis_cutoff_ts = ?, confirmation = NULL WHERE draft_id = ?""",
            (content.sub_account_id, content.instrument_id, content.model_dump_json(), basis, draft_id),
        )
        return self.get(draft_id)

    def confirm(self, draft_id: str, confirmation: DraftConfirmation) -> DraftRecord:
        record = self.get(draft_id)
        if (record.version, record.content_hash) != (confirmation.version, confirmation.content_hash):
            raise DraftConflict("草案版本已更新，请重新审阅后确认")
        if record.status == "CONFIRMED":
            return record
        basis, created = self._db.execute(
            "SELECT basis_cutoff_ts, created_ts FROM paper_drafts WHERE draft_id = ?", (draft_id,),
        ).fetchone()
        confirmed = ConfirmedDraft(
            **confirmation.model_dump(), draft_id=draft_id, content=record.content, basis_cutoff_ts=basis,
            created_ts=created, confirmed_ts=max(created, self._now()),
        )
        with write_transaction(self._db):
            self._db.execute("INSERT INTO paper_submissions(draft_id, version) VALUES (?, ?)", (draft_id, record.version))
            self._db.execute(
                "UPDATE paper_drafts SET confirmation = ? WHERE draft_id = ?", (confirmed.model_dump_json(), draft_id),
            )
        return self.get(draft_id)

    def claim(self, account: str, instrument: str) -> ConfirmedDraft | None:
        row = self._db.execute(
            """SELECT draft_id, confirmation, claimed FROM paper_drafts JOIN paper_submissions USING (draft_id, version)
               WHERE sub_account_id = ? AND instrument_id = ? AND confirmation IS NOT NULL AND receipt IS NULL
               ORDER BY claimed DESC, seq LIMIT 1""",
            (account, instrument),
        ).fetchone()
        if row is None:
            return None
        draft_id, confirmation, claimed = row
        if not claimed:
            self._db.execute("UPDATE paper_drafts SET claimed = 1 WHERE draft_id = ?", (draft_id,))
        return ConfirmedDraft.model_validate_json(confirmation)

    def acknowledge(self, account: str, receipt: DraftReceipt) -> DraftReceipt:
        if receipt.reason in {"DRAFT_INTERRUPTED", "DRAFT_DATA_UNAVAILABLE"}:
            raise DraftConflict("缺证据或处理中断的草案仍待处理")
        record = self.get(receipt.draft_id)
        if not record.claimed or (record.content.sub_account_id, record.version) != (account, receipt.version):
            raise DraftConflict("回执与已送达的草案版本不一致")
        if record.receipt is not None:
            if record.receipt != receipt:
                raise DraftConflict("草案已登记另一份回执")
            return record.receipt
        self._db.execute(
            "UPDATE paper_drafts SET receipt = ? WHERE draft_id = ?", (receipt.model_dump_json(), receipt.draft_id),
        )
        return receipt
