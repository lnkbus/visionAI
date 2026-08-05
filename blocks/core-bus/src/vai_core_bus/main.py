"""CORE-BUS 진입점 — ``python -m vai_core_bus.main``."""

from __future__ import annotations

from vai_common.service import serve
from vai_core_bus.app import create_app

app = create_app()


def main() -> None:
    serve(app, default_port=8081)


if __name__ == "__main__":
    main()
