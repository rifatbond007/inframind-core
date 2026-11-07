"""`Anomaly` lives in `inframind.common.anomaly` (the RCA stage may import only `common`).

Re-exported here so the import path in standard 05 works.
"""

from __future__ import annotations

from inframind.common.anomaly import Anomaly

__all__ = ["Anomaly"]
