import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_systemd_unit_uses_stable_wsgi_target() -> None:
    unit = (ROOT / "deploy/templates/systemd.service.j2").read_text()
    assert "config.wsgi:application" in unit
    assert "granian.utils" not in unit
    assert "TimeoutStopSec=30" in unit


def test_legacy_deploy_requires_live_http_health() -> None:
    for relative_path in ("deploy/deploy.yml", "deploy/linux_macmini_deploy.yml"):
        playbook = (ROOT / relative_path).read_text()
        assert "Wait for the Nyxmon web service to become healthy" in playbook
        assert 'url: "http://127.0.0.1:{{ app_port }}/"' in playbook
        assert 'Host: "{{ fqdn }}"' in playbook
        assert "X-Forwarded-Proto: https" in playbook
        assert "status_code: 200" in playbook


def test_legacy_monitor_unit_keeps_telegram_secrets_out_of_systemd() -> None:
    unit = (ROOT / "deploy/templates/monitor.service.j2").read_text()
    assert "EnvironmentFile={{ site_path }}/.env" in unit
    assert "TELEGRAM_BOT_TOKEN" not in unit
    assert "TELEGRAM_CHAT_ID" not in unit
    assert "--enable-telegram --log-level WARNING" in unit


def test_legacy_deploys_enable_both_nyxmon_units() -> None:
    task_names = (
        "Make sure monitoring agent is running first",
        "Make sure granian service is running",
    )
    for relative_path in ("deploy/deploy.yml", "deploy/linux_macmini_deploy.yml"):
        playbook = (ROOT / relative_path).read_text()
        for task_name in task_names:
            task = re.search(
                rf"(?ms)^    - name: {re.escape(task_name)}\n"
                r"(?P<body>.*?)(?=^    - name: |\Z)",
                playbook,
            )
            assert task is not None
            assert "enabled: true" in task.group("body")
