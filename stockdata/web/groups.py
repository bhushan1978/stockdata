"""Saved symbol groups, including the automatic ALPHABET groups.

The alphabet groups split the loaded universe by the first character of the symbol (``Alphabet A`` ... ``Alphabet Z``, ``Alphabet
0-9`` for symbols that start with a digit, ``Alphabet other`` for anything else) so a large backfill can be done a few letters at a
time. They are rebuilt from the instrument master whenever it changed, are marked ``kind = 'alphabet'`` so they never touch a group
a person made, and a job that used one keeps the security ids it was created with (deleting or rebuilding a group changes no job).
"""
from __future__ import annotations

import hashlib
import json
import string

from stockdata.web.store import Store, now

PREFIX = "Alphabet "
FINGERPRINT_KEY = "alphabet_groups_fp"


def alphabet_key(symbol: str | None) -> str:
    first = (symbol or "").strip()[:1].upper()
    if first in string.ascii_uppercase and first:
        return first
    if first.isdigit():
        return "0-9"
    return "other"


def _sort_key(name: str) -> tuple:
    k = name[len(PREFIX):]
    return (0, k) if len(k) == 1 and k.isalpha() else (1, k)


def alphabet_members(master_rows: list[dict]) -> dict[str, list[int]]:
    """security ids of the ACTIVE instruments of the universe, by initial (de-duplicated, in symbol order)."""
    out: dict[str, list[int]] = {}
    for r in master_rows:
        if r.get("is_active") is False:
            continue
        ids = out.setdefault(alphabet_key(r.get("symbol")), [])
        if r["security_id"] not in ids:
            ids.append(r["security_id"])
    return out


def _fingerprint(members: dict[str, list[int]]) -> str:
    return hashlib.sha256(json.dumps({k: sorted(v) for k, v in sorted(members.items())}).encode()).hexdigest()[:16]


def sync_alphabet_groups(store: Store, master_rows: list[dict], *, force: bool = False) -> dict:
    """Create or update the alphabet groups from the master. Returns what changed. Does nothing when the master has not changed
    since the last sync (unless forced) or when no universe is loaded."""
    members = alphabet_members(master_rows)
    if not members:
        return {"changed": False, "created": [], "updated": [], "removed": [], "skipped": [], "reason": "no instrument universe is loaded"}
    fp = _fingerprint(members)
    have = {g["name"]: g for g in store.all("SELECT id, name, security_ids, kind FROM symbol_groups")}
    if not force and store.get_meta(FINGERPRINT_KEY) == fp and any(g["kind"] == "alphabet" for g in have.values()):
        return {"changed": False, "created": [], "updated": [], "removed": [], "skipped": []}
    created, updated, removed, skipped = [], [], [], []
    for key, ids in members.items():
        name = PREFIX + key
        existing = have.get(name)
        payload = json.dumps(ids)
        if existing is None:
            store.execute("INSERT INTO symbol_groups (name, security_ids, created_at, kind) VALUES (?, ?, ?, 'alphabet')", (name, payload, now()))
            created.append(name)
        elif existing["kind"] != "alphabet":
            skipped.append(name)                                           # a group the person made with this name is never overwritten
        elif existing["security_ids"] != payload:
            store.execute("UPDATE symbol_groups SET security_ids = ? WHERE id = ?", (payload, existing["id"]))
            updated.append(name)
    for name, g in have.items():                                           # an initial that no longer has any instrument
        if g["kind"] == "alphabet" and name.startswith(PREFIX) and name[len(PREFIX):] not in members:
            store.execute("DELETE FROM symbol_groups WHERE id = ?", (g["id"],))
            removed.append(name)
    store.set_meta(FINGERPRINT_KEY, fp)
    changed = bool(created or updated or removed)
    if changed:
        store.audit("group.alphabet.sync", None, {"created": len(created), "updated": len(updated), "removed": len(removed), "skipped": skipped})
    return {"changed": changed, "created": created, "updated": updated, "removed": removed, "skipped": skipped}


def list_groups(store: Store) -> list[dict]:
    rows = store.all("SELECT * FROM symbol_groups")
    out = []
    for g in rows:
        ids = json.loads(g["security_ids"])
        out.append({"id": g["id"], "name": g["name"], "count": len(ids), "security_ids": ids, "created_at": g["created_at"],
                    "kind": g["kind"] or "user"})
    out.sort(key=lambda g: ((0, _sort_key(g["name"])) if g["kind"] == "alphabet" else (1, (0, g["name"].lower()))))
    return out
