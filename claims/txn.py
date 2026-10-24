"""
All-or-nothing multi-document writes for the pickup handover.

On a MongoDB deployment that supports multi-document transactions (replica set,
sharded cluster, Atlas) the work runs inside `session.with_transaction`, so a
failure aborts everything. On a standalone server (and in tests) the same work
runs with compensating rollback: every write made through `Tx` records how to
undo it, and on any exception the undo log is replayed in reverse so no partial
handover persists. All writes are compare-and-set, so the documents involved are
not being changed by anyone else while the handover runs.
"""
import logging

from pymongo import MongoClient, ReturnDocument

logger = logging.getLogger(__name__)

_TRANSACTIONAL_TOPOLOGIES = {"ReplicaSetWithPrimary", "Sharded", "LoadBalanced"}


class ConflictAbort(Exception):
    """Raised inside a unit of work when a compare-and-set did not match."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def supports_transactions(db) -> bool:
    client = getattr(db, "client", None)
    if not isinstance(client, MongoClient):
        return False
    try:
        return client.topology_description.topology_type_name in _TRANSACTIONAL_TOPOLOGIES
    except Exception:
        return False


class Tx:
    def __init__(self, db, session):
        self.db = db
        self.session = session
        self._undo = []

    def _kw(self):
        return {"session": self.session} if self.session is not None else {}

    def cas_update(self, collection: str, query: dict, update: dict, reason: str) -> dict:
        """find_one_and_update that must match; returns the AFTER document."""
        before = self.db[collection].find_one_and_update(query, update, return_document=ReturnDocument.BEFORE,
                                                         **self._kw())
        if before is None:
            raise ConflictAbort(reason)
        if self.session is None:
            self._undo.append(("restore", collection, before))
        return self.db[collection].find_one({"_id": before["_id"]}, **self._kw())

    def insert(self, collection: str, doc: dict):
        result = self.db[collection].insert_one(doc, **self._kw())
        if self.session is None:
            self._undo.append(("delete", collection, result.inserted_id))
        return result.inserted_id

    def rollback(self):
        for op, collection, value in reversed(self._undo):
            try:
                if op == "restore":
                    self.db[collection].replace_one({"_id": value["_id"]}, value)
                else:
                    self.db[collection].delete_one({"_id": value})
            except Exception:
                logger.exception("Rollback step failed for %s in %s", op, collection)
        self._undo.clear()


def run_atomically(db, work):
    """Run work(tx) all-or-nothing and return its result."""
    if supports_transactions(db):
        with db.client.start_session() as session:
            return session.with_transaction(lambda s: work(Tx(db, s)))
    tx = Tx(db, None)
    try:
        return work(tx)
    except BaseException:
        tx.rollback()
        raise
