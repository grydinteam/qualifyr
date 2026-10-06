"""SSRF guard: the crawler must refuse non-public IP-literal hosts (cloud metadata, loopback,
private ranges) and still allow public hosts. Hermetic – no DNS, no network: the guard works on
IP literals only and short-circuits before any request is made."""

from gtm_engine.config.schema import EngineSettings
from gtm_engine.scraping.fetcher import HttpFetcher, _is_blocked_ip_literal


def test_blocks_non_public_ip_literals():
    for host in ["169.254.169.254", "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1",
                 "::1", "0.0.0.0", "fd00::1",
                 # IPv6-mapped IPv4: the mapped form's own is_loopback/is_private are False on
                 # older Python, so these must be unwrapped and blocked by the embedded IPv4.
                 "::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:10.0.0.1"]:
        assert _is_blocked_ip_literal(host), f"{host} should be blocked"


def test_allows_public_ips_and_domains():
    # Public IPs and ANY domain name (domains are not resolved here, by design).
    for host in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "example.com", "khaadi.com"]:
        assert not _is_blocked_ip_literal(host), f"{host} should be allowed"


async def test_fetcher_short_circuits_metadata_endpoint():
    """The classic SSRF target: the cloud metadata IP. Must be refused before any request,
    so the result carries the block marker and a zero status (no network hit)."""
    async with HttpFetcher(EngineSettings()) as f:
        r = await f.get("http://169.254.169.254/latest/meta-data/")
    assert r.error == "blocked_private_host"
    assert r.status_code == 0
