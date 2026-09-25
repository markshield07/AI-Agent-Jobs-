"""Launch options: a proxy from the environment never catches local pages."""

from __future__ import annotations

from jobagent.apply.browser.session import launch_options, proxy_bypass
from jobagent.config import Settings


def test_no_proxy_hosts_and_this_machine_bypass_the_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "corp.example, 10.0.0.0/8,*,localhost")
    monkeypatch.delenv("no_proxy", raising=False)
    options = launch_options(Settings(data_dir=tmp_path))
    assert options["proxy"]["server"] == "http://proxy.example:3128"
    assert options["proxy"]["bypass"] == "corp.example,10.0.0.0/8,localhost,127.0.0.1,::1"


def test_no_proxy_means_no_proxy_option(monkeypatch, tmp_path):
    for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert "proxy" not in launch_options(Settings(data_dir=tmp_path))
    assert proxy_bypass() == "localhost,127.0.0.1,::1"
