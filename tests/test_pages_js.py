"""The pages' inline JavaScript must parse: a stray quote in a help text silently breaks a whole page."""

import re
import shutil
import subprocess

import pytest
from starlette.testclient import TestClient

from community_radar.cli import add_routes
from community_radar.server import create_server

NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="Node.js is needed to check the pages' JavaScript")
@pytest.mark.parametrize("path", ["/sources", "/triage?days=30"])
def test_page_scripts_parse(collector, docs, path, tmp_path):
    server = create_server(collector, docs)
    add_routes(server, collector, docs)
    page = TestClient(server.streamable_http_app()).get(path).text
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts
    script = tmp_path / "page.js"
    script.write_text("\n".join(scripts))
    result = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
