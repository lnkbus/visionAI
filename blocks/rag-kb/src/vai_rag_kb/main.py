"""RAG-KB 진입점."""

from __future__ import annotations

from vai_common.service import serve
from vai_rag_kb.app import create_app

app = create_app()


def main() -> None:
    serve(app, default_port=8086)


if __name__ == "__main__":
    main()
