"""AUD-RTP 진입점."""

from __future__ import annotations

from vai_aud_rtp.app import create_app
from vai_common.service import serve

app = create_app()


def main() -> None:
    serve(app, default_port=8092)


if __name__ == "__main__":
    main()
