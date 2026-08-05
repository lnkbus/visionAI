"""UI-AGENT 진입점."""

from __future__ import annotations

from vai_common.service import serve
from vai_ui_agent.app import create_app

app = create_app()


def main() -> None:
    serve(app, default_port=8091)


if __name__ == "__main__":
    main()
