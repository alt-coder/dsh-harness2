"""The editable surface a DSH agent actually has.

DSH discovers project instructions from `AGENTS.md` and project skills from
`.agents/skills/<name>/SKILL.md` (see `dsh-skill-filesystem` roots: rank 200
`project-agents`), runs helpers through its bash tool, and delegates work to
subagents. That is the same shape Harness2 calls a workspace-agent harness, so the
surface mirrors the shipped codex one, minus `.codex/hooks.json`: DSH has no
hook file of that schema.
"""

from __future__ import annotations

from harness2.core.tree import SurfaceSpec

DSH_SURFACE = SurfaceSpec(
    name="dsh",
    always_on=("AGENTS.md",),
    always_on_max=8_000,
    always_on_cap_reason="AGENTS.md is injected whole into every turn; every token competes "
    "with the agent's own output budget",
    skill_dir=".agents/skills",
    # DSH drops a skill whose frontmatter has no `name:` or a `name:` that does not match
    # its directory, so the strict rule is the honest one here.
    skill_name_rule="must_match",
    trigger_hint=(
        'Fix it by adding a trigger line naming `{name}` to AGENTS.md, e.g. "When '
        "<the situation this skill handles>, read .agents/skills/{name}/SKILL.md "
        'and follow it."'
    ),
    autotrigger_file="AGENTS.md",
    trigger_line="- When {desc}, read .agents/skills/{name}/SKILL.md and follow it.\n",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
    compile_skill_scripts=True,
    reject_empty_always_on=True,
    stubs=(
        (
            "scripts/README.md",
            "<!-- c7: deterministic helpers, run as `python3 scripts/<name>.py`. "
            "Add one by emitting scripts/<name>.py. -->\n",
        ),
        (
            "sub_agents/README.md",
            "<!-- c5: delegated roles, each spawned in its OWN context. Add one as "
            "sub_agents/<name>/AGENT.md with frontmatter `description:`. -->\n",
        ),
        (
            ".agents/skills/README.md",
            "<!-- c6: reusable workflows. Add one as .agents/skills/<name>/SKILL.md with YAML "
            "frontmatter: BOTH `name:` (matching the directory) and `description:` are "
            "required, or the skill is dropped at load. Only name+description are advertised; "
            "the body is read on demand. -->\n",
        ),
    ),
)


def dsh_tree_entries() -> tuple[str, ...]:
    """Top-level entries a DSH workspace harness carries into a rollout."""
    return ("AGENTS.md", ".agents", "scripts", "sub_agents")


__all__ = ["DSH_SURFACE", "dsh_tree_entries"]
