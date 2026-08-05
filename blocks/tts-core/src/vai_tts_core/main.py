"""TTS-CORE 진입점."""

from __future__ import annotations

from vai_common.service import serve
from vai_tts_core.app import create_app

app = create_app()


def main() -> None:
    serve(app, default_port=8098)


if __name__ == "__main__":
    main()
