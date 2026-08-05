"""CORE-LIC 진입점."""

from __future__ import annotations

from vai_common.service import serve
from vai_core_lic.app import create_app

app = create_app()


def main() -> None:
    serve(app, default_port=8095)


if __name__ == "__main__":
    main()
