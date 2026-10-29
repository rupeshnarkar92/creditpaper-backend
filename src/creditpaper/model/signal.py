"""Signal — re-exported from `creditpaper.contracts`.

Signals cross the engine/platform boundary: the review surface creates them and
the learning substrate consumes them, so the type is part of the agreed contract
rather than an engine internal. Kept here as a re-export because the engine has
always imported it from `model.signal`.
"""

from __future__ import annotations

from creditpaper.contracts.signal import Signal, SignalKind

__all__ = ["Signal", "SignalKind"]
