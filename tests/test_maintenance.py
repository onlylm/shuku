from __future__ import annotations

import json
import socket
import time

import pytest

from scripts import maintenance_protocol as protocol
from scripts import server_maintenance as host
from scripts.server_config import create_config, make_config, read_config


def job(kind="backup", payload=None):
    return {"id": "a"*32, "protocol": 1, "kind": kind, "payload": payload or {}, "created_at": time.time()}


@pytest.mark.parametrize("payload", [
    {"command": "whoami"}, {"path": "/root"}, {"path": "../../etc/passwd"},
])
def test_backup_rejects_arbitrary_paths_commands(payload):
    with pytest.raises(ValueError):
        protocol.validate_job(job(payload=payload))


@pytest.mark.parametrize("tag", ["main", "--upload-pack=x", "v2.0.0;whoami", "v2.0.0-beta", "../HEAD"])
def test_updates_only_accept_stable_pinned_versions(tag):
    with pytest.raises(ValueError):
        protocol.validate_job(job("update", {"tag": tag, "sha": "a"*40}))


def test_protocol_expiry_and_shape():
    value = job()
    value["created_at"] -= 901
    with pytest.raises(ValueError):
        protocol.validate_job(value)
    value = job()
    value["id"] = "../../bad"
    with pytest.raises(ValueError):
        protocol.validate_job(value)
    assert protocol.validate_job(job("update", {"tag": "v2.0.0", "sha": "a"*40}))["kind"] == "update"


def test_release_is_stable_and_immutable_commit(monkeypatch):
    calls = []
    def api(path):
        calls.append(path)
        return {"sha": "a"*40} if path.startswith("/commits/") else {"tag_name": "v2.0.0", "body": "改进", "draft": False, "prerelease": False}
    monkeypatch.setattr(protocol, "github_json", api)
    monkeypatch.setattr(protocol, "current_version", lambda: "2.0.0-dev")
    result = protocol.release_info()
    assert result["available"] and result["sha"] == "a"*40
    assert calls == ["/releases/latest", "/commits/v2.0.0"]
    monkeypatch.setattr(protocol, "github_json", lambda _: {"tag_name": "v2.0.0", "prerelease": True})
    with pytest.raises(ValueError):
        protocol.release_info()


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "192.168.0.1", "169.254.169.254", "::1", "fc00::1", "0.0.0.0"])
def test_domain_validation_rejects_private_addresses(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, (ip, 443))])
    with pytest.raises(ValueError, match="公网"):
        host.public_addresses("books.example.com")


def test_alias_config_normalized_and_no_injection():
    output = host.alias_config("books.example.com", ["www.example.com", "books.example.com", "www.example.com"])
    assert output.count("www.example.com") == 1
    assert "redir https://books.example.com{uri} 308" in output
    with pytest.raises(ValueError):
        host.alias_config("books.example.com", ["x.example.com\nimport /root/*"])


@pytest.fixture()
def worker(tmp_path, monkeypatch):
    values = make_config("old.example.com", "测试网站", "admin")
    create_config(tmp_path / "deploy/.env", values)
    obj = host.HostMaintenance(tmp_path)
    obj.domains.mkdir()
    (obj.control / "requests").mkdir(parents=True)
    (obj.control / "status").mkdir()
    obj.job = job()
    calls = []
    monkeypatch.setattr(obj, "compose", lambda *args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(obj, "reload_caddy", lambda: calls.append(("reload",)))
    obj.calls = calls
    return obj


def test_domains_validate_before_switch_and_keep_old_alias(worker, monkeypatch):
    payload = {"primary": "new.example.com", "aliases": ["www.example.com"], "previous_primary": "old.example.com"}
    worker.job = job("domains", payload)
    monkeypatch.setattr(host, "public_addresses", lambda _: ["8.8.8.8"])
    def verify(domain, path, headers=None):
        if path == "/api/v1/domain-verification":
            assert read_config(worker.config)["SITE_DOMAIN"] == "old.example.com"
            nonce = (worker.domains / "verify.caddy").read_text(encoding="utf-8").split('"')[1]
            return 200, nonce.encode()
        return 200, b"ready"
    monkeypatch.setattr(host, "https_get", verify)
    worker.apply_domains(payload)
    values = read_config(worker.config)
    assert values["SITE_DOMAIN"] == "new.example.com"
    assert values["SITE_ALIASES"] == "www.example.com,old.example.com"
    assert worker.result["status"] == "completed"


def test_bad_domain_never_replaces_old_domain(worker, monkeypatch):
    payload = {"primary": "new.example.com", "aliases": [], "previous_primary": "old.example.com"}
    worker.job = job("domains", payload)
    monkeypatch.setattr(host, "public_addresses", lambda _: ["8.8.8.8"])
    monkeypatch.setattr(host, "https_get", lambda *a, **k: (200, b"another website"))
    monkeypatch.setattr(host.time, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="原主域名"):
        worker.apply_domains(payload)
    assert read_config(worker.config)["SITE_DOMAIN"] == "old.example.com"


def test_changed_domain_config_requires_new_confirmation(worker):
    with pytest.raises(ValueError, match="重新确认"):
        worker.apply_domains({"primary": "new.example.com", "aliases": [], "previous_primary": "stale.example.com"})


def setup_update(worker, monkeypatch, fail_backup=False, fail_deploy=False, fail_restore=False):
    payload = {"tag": "v2.0.0", "sha": "a"*40}
    worker.job = job("update", payload)
    calls = worker.calls
    monkeypatch.setattr(host, "current_version", lambda: "2.0.0-dev")
    monkeypatch.setattr(host, "release_info", lambda _: payload)
    def git(*args, **kwargs):
        calls.append(("git", *args))
        if args == ("status", "--porcelain"):
            return ""
        if args == ("rev-parse", "FETCH_HEAD"):
            return "a"*40
        if args[0] == "show":
            return "2.0.0"
        if args == ("rev-parse", "HEAD"):
            return "b"*40
        return ""
    monkeypatch.setattr(worker, "git", git)
    def command(args, **kwargs):
        calls.append(tuple(args))
        if "maintenance-deploy" in args and fail_deploy:
            raise ValueError("模拟升级失败")
        return "test-image"
    monkeypatch.setattr(worker, "command", command)
    def backup(keep_stopped=False):
        calls.append(("backup", keep_stopped))
        if fail_backup:
            raise ValueError("模拟备份失败")
        return worker.root / "backups/snapshot"
    monkeypatch.setattr(worker, "backup", backup)
    monkeypatch.setattr(worker, "maintenance_gate", lambda nonce: calls.append(("gate", bool(nonce))))
    monkeypatch.setattr(worker, "smoke", lambda nonce: calls.append(("smoke",)))
    def restore(*args):
        calls.append(("restore",))
        if fail_restore:
            raise ValueError("模拟恢复失败")
    monkeypatch.setattr(worker, "restore_snapshot", restore)
    return payload


def test_failed_backup_prevents_code_change(worker, monkeypatch):
    payload = setup_update(worker, monkeypatch, fail_backup=True)
    with pytest.raises(ValueError, match="备份失败"):
        worker.update_release(payload)
    assert not any(c[:2] == ("git", "switch") for c in worker.calls)
    assert ("gate", False) in worker.calls


def test_failed_update_restores_before_opening_site(worker, monkeypatch):
    payload = setup_update(worker, monkeypatch, fail_deploy=True)
    worker.update_release(payload)
    assert worker.result["status"] == "rolled_back"
    assert worker.calls.index(("restore",)) < worker.calls.index(("gate", False))
    assert not worker.recovery_required


def test_failed_recovery_keeps_maintenance_and_blocks_new_jobs(worker, monkeypatch):
    payload = setup_update(worker, monkeypatch, fail_deploy=True, fail_restore=True)
    with pytest.raises(ValueError, match="恢复失败"):
        worker.update_release(payload)
    assert ("gate", False) not in worker.calls
    assert worker.recovery_required


def test_successful_update_is_verified_before_opening(worker, monkeypatch):
    payload = setup_update(worker, monkeypatch)
    worker.update_release(payload)
    assert worker.result["status"] == "completed"
    assert worker.calls.index(("smoke",)) < worker.calls.index(("gate", False))


def test_dirty_source_is_not_overwritten(worker, monkeypatch):
    payload = setup_update(worker, monkeypatch)
    monkeypatch.setattr(worker, "git", lambda *a, **k: " M app/main.py")
    with pytest.raises(ValueError, match="未提交修改"):
        worker.update_release(payload)
    assert worker.calls == []


def test_no_implicit_job_on_heartbeat(worker):
    worker.run_one()
    assert not worker.calls
    assert json.loads((worker.control / "status/heartbeat.json").read_text())["protocol"] == 1


def test_job_processed_once_and_status_saved(worker, monkeypatch):
    monkeypatch.setattr(worker, "backup", lambda: worker.root / "snapshot")
    (worker.control / "requests/pending.json").write_text(json.dumps(job()))
    worker.run_one()
    assert not (worker.control / "requests/pending.json").exists()
    assert not (worker.control / "status/active.json").exists()
    assert json.loads((worker.control / ("status/job-"+"a"*32+".json")).read_text(encoding="utf-8"))["status"] == "completed"


def test_interrupted_job_is_not_replayed(worker):
    (worker.control / "status/active.json").write_text(json.dumps(job()))
    worker.run_one()
    assert not worker.calls
    assert (worker.control / "status/active.json").exists()
    assert worker.result["status"] == "failed"
