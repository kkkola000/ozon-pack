"""Regression checks for nginx-only IP filtering and migration of existing installs."""
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import shutil
import socket
import subprocess
import threading
import time

import pytest

from app.core.config import BASE_DIR

spec = importlib.util.spec_from_file_location("nginx_access", BASE_DIR / "deploy/nginx-access.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def setup_policy(tmp_path, old="", nginx=None):
    (tmp_path / ".env").write_text("IP_ALLOWLIST=" + old + "\n")
    snippet = tmp_path / "access.conf"
    if nginx is not None:
        snippet.write_text(nginx)
    return snippet


def test_migration_preserves_intersection(tmp_path):
    snippet = setup_policy(tmp_path, "10.8.0.0/16,203.0.113.10",
                           "allow 10.8.1.0/24;\ndeny all;\n")
    policy.write_policy(tmp_path, snippet)
    assert "allow 10.8.1.0/24;" in snippet.read_text()
    assert "allow 10.8.0.0/16;" not in snippet.read_text()
    assert "203.0.113.10" not in snippet.read_text()
    before = snippet.read_text()
    policy.write_policy(tmp_path, snippet)
    assert snippet.read_text() == before


def test_disjoint_policies_never_become_allow_all(tmp_path):
    snippet = setup_policy(tmp_path, "10.8.0.0/24", "allow 192.0.2.5;\ndeny all;\n")
    policy.write_policy(tmp_path, snippet)
    assert "allow all;" not in snippet.read_text()
    assert snippet.read_text().endswith("deny all;\n")


@pytest.mark.parametrize("bad", ["bad-address", "10.8.0.0/24\ndeny all", "10.8.0.0/99"])
def test_invalid_policy_does_not_overwrite_existing_rule(tmp_path, bad):
    snippet = setup_policy(tmp_path, bad, "allow all;\n")
    with pytest.raises(ValueError):
        policy.write_policy(tmp_path, snippet)
    assert snippet.read_text() == "allow all;\n"


def test_custom_rule_is_not_silently_broadened(tmp_path):
    snippet = setup_policy(tmp_path, "10.8.0.0/24", "deny 10.8.0.5;\nallow all;\n")
    with pytest.raises(ValueError):
        policy.write_policy(tmp_path, snippet)
    assert "deny 10.8.0.5;" in snippet.read_text()


def test_existing_nginx_rule_survives_empty_environment(tmp_path):
    snippet = setup_policy(tmp_path, nginx="allow 192.0.2.5;\ndeny all;\n")
    before = snippet.read_text()
    policy.write_policy(tmp_path, snippet)
    assert snippet.read_text() == before


def test_every_proxy_location_must_include_the_acl(tmp_path):
    snippet = setup_policy(tmp_path)
    site = tmp_path / "site.conf"
    site.write_text(f"""
server {{
  location / {{ include {snippet}; proxy_pass http://127.0.0.1:8080; }}
  location /api/ {{ proxy_pass http://127.0.0.1:8080; }}
}}
""")
    with pytest.raises(ValueError, match="не подключает"):
        policy.verify_site(site, snippet, "8080")


def test_additional_allow_after_proxy_is_rejected(tmp_path):
    snippet = setup_policy(tmp_path)
    site = tmp_path / "site.conf"
    site.write_text(f"server {{ location / {{ include {snippet}; proxy_pass http://127.0.0.1:8080; allow all; }} }}")
    with pytest.raises(ValueError, match="директивы"):
        policy.verify_site(site, snippet, "8080")


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


@pytest.mark.skipif(not shutil.which("nginx"), reason="nginx is required for integration test")
def test_real_nginx_denies_spoofed_ip_and_forwards_real_ip(tmp_path):
    class Backend(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(dict(self.headers)).encode())

        def log_message(self, *_args):
            pass

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    worker = threading.Thread(target=backend.serve_forever, daemon=True)
    worker.start()
    port = free_port()
    snippet = tmp_path / "access.conf"
    # Deliberately do not allow 127.0.0.1: a forged XFF must not bypass nginx.
    snippet.write_text("allow 127.0.0.2;\ndeny all;\n")
    config = tmp_path / "nginx.conf"
    config.write_text(f"""
pid {tmp_path}/nginx.pid;
error_log stderr;
events {{ worker_connections 32; }}
http {{
  access_log off;
  server {{
    listen 127.0.0.1:{port};
    location /.well-known/acme-challenge/ {{ return 200 "challenge"; }}
    location / {{
      include {snippet};
      proxy_set_header X-Forwarded-For $remote_addr;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_pass http://127.0.0.1:{backend.server_port};
    }}
  }}
}}
""")
    process = subprocess.Popen(["nginx", "-p", str(tmp_path), "-c", str(config),
                                "-g", "daemon off;"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            pytest.fail("nginx did not start")
        def request(source, path="/"):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3,
                                                     source_address=(source, 0))
            try:
                connection.request("GET", path, headers={"X-Forwarded-For": "127.0.0.2"})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        assert request("127.0.0.1")[0] == 403
        status, body = request("127.0.0.2")
        assert status == 200
        assert json.loads(body)["X-Forwarded-For"] == "127.0.0.2"
        assert request("127.0.0.1", "/.well-known/acme-challenge/check")[0] == 200
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        backend.shutdown()
        backend.server_close()
        worker.join(timeout=2)

