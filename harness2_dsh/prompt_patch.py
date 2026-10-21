"""Make Harness2's editor prompts describe the DSH surface.

Harness2 keys its per-substrate prompt table by surface name (`opencode`, `codex`),
so a substrate named `dsh` needs an entry. This reuses the shipped workspace-agent
text and removes the `.codex/hooks.json` component, which DSH does not have — the
only upstream behaviour it changes is that table, at import time, in this process.
"""

from __future__ import annotations

from harness2.core import prompts as hp

HOOKS_PATH = ".codex/hooks.json"


def _strip_hooks(text: str) -> str:
    """Drop table rows and paragraphs that describe the codex hook file."""
    kept: list[str] = []
    for para in text.split("\n\n"):
        if HOOKS_PATH in para:
            continue
        kept.append(para)
    out = "\n\n".join(kept)
    if HOOKS_PATH in out:  # a table row sharing a paragraph with other rows
        out = "\n".join(line for line in out.splitlines() if HOOKS_PATH not in line)
    return out


def install() -> None:
    """Register the `dsh` prompt surface once, in this process."""
    if "dsh" in hp._SURFACE_TEXTS:
        return
    surface_md = _strip_hooks(hp._SURFACE_CODEX)
    if HOOKS_PATH in surface_md:
        raise RuntimeError(
            f"the codex surface text no longer isolates {HOOKS_PATH}; update prompt_patch.py"
        )
    codex = hp._SURFACE_TEXTS["codex"]
    hp._SURFACE_TEXTS["dsh"] = hp._SurfaceText(
        surface_md=surface_md,
        inv=hp._INV_TEMPLATE.replace(
            "__LOOP_LEVER__",
            "an always-on rule in `AGENTS.md` -- for example, requiring the "
            "{deliverable} to be written before the run may finish",
        ),
        dom=hp._DOM_TEMPLATE.replace(
            "__TRIGGER_SENTENCE__",
            "A skill exists only once its name appears in `AGENTS.md` as a trigger: write "
            "that trigger line in the same edit.",
        ),
        tts_surface=codex.tts_surface,
    )


__all__ = ["install"]
