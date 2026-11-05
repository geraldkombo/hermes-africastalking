"""Africa's Talking platform plugin for Hermes Agent (SMS + USSD).

Import-light: the adapter module (and its gateway/aiohttp imports) is pulled
inside ``register()`` so plugin discovery and the offline test suite never pay
the import cost — per the platform-plugin guide's recommendation.
"""

from pathlib import Path


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    from .adapter import register as _register

    _register(ctx)

    # Bundle the setup skill (read-only, namespaced "africastalking:<skill>").
    register_skill = getattr(ctx, "register_skill", None)
    skills_dir = Path(__file__).parent / "skills"
    if register_skill is not None and skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                register_skill(child.name, skill_md)


__all__ = ["register"]
