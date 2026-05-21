"""Entrypoint: ``python -m polyecho.detect``.

Launches the aiohttp service on 0.0.0.0:8001. The container is reached
internally over the docker network by the Next.js web service (no public
port published — see Dockerfile.detect / compose service block).
"""

from polyecho.detect.app import run

if __name__ == "__main__":
    run()
