"""Packaged EC2 runtime assets for UDA environments."""

from pathlib import Path


EC2_RUNTIME_ROOT = Path(__file__).resolve().parent
ENV_PROFILES_PATH = EC2_RUNTIME_ROOT / "env_profiles.yaml"
