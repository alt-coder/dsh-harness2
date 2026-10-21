"""Small helpers that keep the CLI free of channel-plumbing details."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness2.core.spec import ChannelSpec


def channel_for(model: str) -> ChannelSpec:
    """The proposer channel this adapter runs on.

    The kind is `opencode` only because that is the transport slot `Proposer.call`
    dispatches on; `DshProposer` replaces the transport itself. The model string is a
    label carried into audit records and prompts.
    """
    return ChannelSpec("opencode", model if "/" in model else f"dsh/{model}")
