from __future__ import annotations

from fastapi.testclient import TestClient

from clawtune_sidecar.api.app import create_app


def main() -> None:
    client = TestClient(create_app())
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    print("sidecar smoke test passed")


if __name__ == "__main__":
    main()
