"""The append-only audit log (D20, D25).

The D20 contract says "no UPDATE, no DELETE" on the audit log. This module is the
application-layer half of that guarantee: the *only* public function is `record_audit`,
which inserts. There is intentionally no `update_audit`, no `delete_audit`, no
`truncate_audit`. A production deployment would also revoke the corresponding SQL
privileges on `audit_log` from the application role; that policy is documented in
`docs/standards/06b-storage-and-alerting.md`.

The implementation is a thin re-export of `storage/incidents.py::record_audit` so the
`record_audit` symbol lives in the place readers expect ("the audit module") while the
actual SQL stays alongside the other inserts for transactional locality.

D25: `actor` is the GitHub username (the local-part of `<username>@users.noreply.github.com`),
never a display name and never an AI tool name. Validation happens at the API boundary
(see `api/app.py`); this module trusts its caller.
"""

from __future__ import annotations

from inframind.storage.incidents import record_audit as _record_audit

record_audit = _record_audit
"""Append one row to `audit_log`.

Parameters mirror the `audit_log` columns; see `storage/incidents.py::record_audit` for
the SQL. The symbol is re-exported here so a caller reading "the audit module" does not
have to know the underlying implementation lives next to the incident writes.
"""

__all__ = ["record_audit"]
