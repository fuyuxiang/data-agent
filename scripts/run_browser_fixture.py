#!/usr/bin/env python3
"""Start an isolated application instance for Playwright integration tests."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from backend import create_app


def main() -> None:
    storage = Path(tempfile.mkdtemp(prefix="meridian-browser-test-"))
    # Keep the browser fixture deterministic even when the aggregate verifier
    # itself carries production Compose variables in its environment.
    os.environ["MERIDIAN_ENV"] = "test"
    os.environ.pop("MERIDIAN_TRUSTED_HOSTS", None)
    os.environ.pop("MERIDIAN_ALLOWED_ORIGINS", None)
    os.environ["MERIDIAN_FRONTEND_DIR"] = str(Path(__file__).resolve().parents[1] / "frontend" / "dist")
    app = create_app({
        "TESTING": True,
        "DATABASE_PATH": storage / "meridian.sqlite3",
        "STORAGE_DIR": storage,
        "SECRET_KEY": "browser-test-secret",
    })
    app.run(host="127.0.0.1", port=5013, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
