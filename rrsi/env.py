"""Shared OpenAI configuration, including standalone benchmark entry points."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from dotenv import load_dotenv


def load_env() -> None:
    root = Path(__file__).resolve().parent.parent
    env_file = root / ".env"
    # Linked worktrees do not contain the ignored .env; use the main checkout.
    if not env_file.is_file() and (root / ".git").is_file():
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        )
        common = (root / result.stdout.strip()).resolve()
        env_file = common.parent / ".env"
    load_dotenv(env_file, override=False)
    os.environ["RRSI_ENV_LOADED"] = "1"


def model_name() -> str:
    return os.environ.get("OPENAI_MODEL_NAME", "").strip()


def litellm_model() -> str:
    # The value is the server's model ID, including any namespace it requires.
    return f"openai/{model_name()}"


def require_openai_env() -> None:
    missing = [key for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL_NAME")
               if not os.environ.get(key, "").strip()]
    if missing:
        raise RuntimeError("Set " + ", ".join(missing) + " in the repository root .env")


load_env()


if __name__ == "__main__":
    require_openai_env()
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python rrsi/env.py COMMAND [ARGS...]")
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
