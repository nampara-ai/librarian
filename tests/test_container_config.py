from pathlib import Path


def test_dockerfile_defines_api_healthcheck() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "HEALTHCHECK" in dockerfile
    assert "http://127.0.0.1:8080/ready" in dockerfile
    assert "urllib.request.urlopen" in dockerfile


def test_dockerfile_runs_as_unprivileged_user() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "adduser --disabled-password" in dockerfile
    assert "chown -R librarian:librarian /data" in dockerfile
    assert "USER librarian" in dockerfile


def test_compose_waits_for_api_health_before_worker_start() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "healthcheck:" in compose
    assert "http://127.0.0.1:8080/ready" in compose
    assert "condition: service_healthy" in compose


def test_compose_drops_container_privileges() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "read_only: true" in compose
    assert compose.count("pids_limit: 512") == 2
    assert "cap_drop:" in compose
    assert "no-new-privileges:true" in compose
    assert "tmpfs:" in compose


def test_compose_defines_restart_and_shutdown_policy() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert compose.count("restart: unless-stopped") == 2
    assert "stop_grace_period: 30s" in compose
    assert "stop_grace_period: 5m" in compose


def test_compose_rotates_container_logs() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert compose.count("driver: json-file") == 2
    assert compose.count('max-size: "10m"') == 2
    assert compose.count('max-file: "5"') == 2
