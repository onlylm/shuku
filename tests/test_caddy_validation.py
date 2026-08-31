"""使用官方 Caddy 解析器检查生成配置；不启动网站或申请真实域名。"""
import os
from pathlib import Path
import subprocess

import pytest

from scripts.server_maintenance import alias_config


@pytest.mark.parametrize("maintenance,staging", [(False, False), (True, False), (False, True)])
def test_caddy_generated_configuration(tmp_path, maintenance, staging):
    binary = os.environ.get("CADDY_BINARY")
    if not binary:
        pytest.skip("未配置 CADDY_BINARY；Linux 容器流水线另行校验正式 Caddyfile")
    root = Path(__file__).parents[1]
    domains = tmp_path / "domains"
    domains.mkdir()
    gate = '@maintenance {\n not header X-Shuku-Maintenance abc123\n}\nrespond @maintenance "Maintenance" 503\n' if maintenance else "# normal\n"
    (domains / "gate.conf").write_text(gate, encoding="utf-8")
    (domains / "aliases.caddy").write_text(alias_config("books.example.com", ["www.example.com"]), encoding="utf-8")
    if staging:
        (domains / "verify.caddy").write_text('new.example.com {\n respond /api/v1/domain-verification "abc123"\n respond "Verifying" 503\n}', encoding="utf-8")
    config = (root / "deploy/Caddyfile").read_text(encoding="utf-8").replace("/etc/caddy/domains", domains.as_posix())
    path = tmp_path / "Caddyfile"
    path.write_text(config, encoding="utf-8")
    result = subprocess.run([binary, "adapt", "--config", str(path), "--adapter", "caddyfile", "--validate"],
        env={**os.environ, "SITE_DOMAIN": "books.example.com"}, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
