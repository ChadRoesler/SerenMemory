"""Detect whether the process is under a service manager that will auto-restart
it after exit. Used to gate the /migrate/restart self-exit: we only self-kill
when something's watching to bring us back. Fails SAFE - when unsure, report
NOT supervised so the UI shows manual-restart instructions instead of stranding
the operator with a dead service."""
from __future__ import annotations
import os


def detect_supervised() -> dict:
    """Return {'supervised': bool, 'signals': {...}}.

    CONFIDENT-true only when our own explicit flag is set (the strongest,
    manager-agnostic signal - setup-service.* sets SEREN_SUPERVISED=1) or a
    strong manager tell (systemd's INVOCATION_ID). Weak signals (launchd's
    XPC_SERVICE_NAME) are recorded but never sufficient alone. Unknown -> False.
    """
    signals = {
        # The flag WE set in every service definition - bulletproof + uniform
        # across NSSM/systemd/launchd because it's intentional, not inferred.
        "seren_supervised_env": os.environ.get("SEREN_SUPERVISED") == "1",
        # systemd sets this for units it launches; very reliable.
        "systemd_invocation_id": bool(os.environ.get("INVOCATION_ID")),
        # launchd sets this, but it can appear elsewhere - weak, never alone.
        "launchd_xpc": bool(os.environ.get("XPC_SERVICE_NAME")),
    }
    confident = signals["seren_supervised_env"] or signals["systemd_invocation_id"]
    return {"supervised": confident, "signals": signals}