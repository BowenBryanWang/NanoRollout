from __future__ import annotations

from nanorollout.envs.uda_env.runtime_profile import _missing_software


def test_required_software_inherits_parent_profile_capabilities() -> None:
    profiles = {
        "base": {
            "includes": ["Google Chrome", "Python 3", "LibreOffice Calc"],
            "services": ["uda-compat"],
        },
        "analytics": {
            "parent": "base",
            "includes": ["pandas", "SQLite Browser"],
        },
    }

    missing = _missing_software(
        ["chrome", "python", "libreoffice", "pandas", "sqlite"],
        "analytics",
        profiles["analytics"],
        profiles,
    )

    assert missing == []


def test_required_software_reports_true_profile_gap() -> None:
    profiles = {"general": {"includes": ["chrome", "python"]}}

    missing = _missing_software(
        ["chrome", "blender"],
        "general",
        profiles["general"],
        profiles,
    )

    assert missing == ["blender"]
