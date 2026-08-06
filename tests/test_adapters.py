import json

from observe.adapters import (JsonlLogAdapter, SystemdTimerAdapter,
                              poll_all)

TIMER_OUT = (
    "Wed 2026-08-06 07:16:00 CDT 20min Wed 2026-08-06 06:56:00 CDT 1min ago"
    "  avara-xmr-swing-paper.timer  avara-xmr-swing-paper.service\n"
    "Sun 2026-08-09 04:17:00 CDT 3 days -                          -"
    "  sgam-log-rotate.timer        sgam-log-rotate.service\n"
)


def test_systemd_adapter_parses_timers():
    adapter = SystemdTimerAdapter(runner=lambda cmd: (0, TIMER_OUT, ""))
    report = adapter.poll(now=1000.0)
    assert report.available is True
    keys = {o.key for o in report.observations}
    assert keys == {"avara-xmr-swing-paper.timer", "sgam-log-rotate.timer"}
    first = next(o for o in report.observations if o.key.startswith("avara"))
    assert first.data["activates"] == "avara-xmr-swing-paper.service"
    assert first.event == "timer_state" and first.ts == 1000.0


def test_systemd_adapter_reports_unavailable_rather_than_empty():
    missing = SystemdTimerAdapter(runner=lambda cmd: (4, "", "Failed to connect"))
    report = missing.poll()
    assert report.available is False and report.observations == []
    assert "not empty" in report.note and "Failed to connect" in (report.error or "")

    def boom(cmd):
        raise FileNotFoundError("systemctl")

    crashed = SystemdTimerAdapter(runner=boom).poll()
    assert crashed.available is False and "FileNotFoundError" in crashed.error


def test_systemd_adapter_uses_user_scope_by_default():
    seen = {}

    def capture(cmd):
        seen["cmd"] = cmd
        return 0, "", ""

    SystemdTimerAdapter(runner=capture).poll()
    assert "--user" in seen["cmd"] and "list-timers" in seen["cmd"]
    SystemdTimerAdapter(user_scope=False, runner=capture).poll()
    assert "--user" not in seen["cmd"]


def test_jsonl_adapter_reads_incrementally(tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps({"kind": "beat", "ts": 1.0, "host": "a"}) + "\n")
    adapter = JsonlLogAdapter(log, key_field="host")

    first = adapter.poll()
    assert first.available is True and len(first.observations) == 1
    assert first.observations[0].event == "beat"
    assert first.observations[0].key == "a"

    second = adapter.poll()          # nothing new
    assert second.available is True and second.observations == []

    with log.open("a") as handle:
        handle.write(json.dumps({"kind": "beat", "ts": 2.0, "host": "b"}) + "\n")
    third = adapter.poll()
    assert [o.key for o in third.observations] == ["b"]


def test_jsonl_adapter_handles_rotation_and_says_so(tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_text("\n".join(json.dumps({"kind": "x", "ts": i})
                             for i in range(5)) + "\n")
    adapter = JsonlLogAdapter(log)
    assert len(adapter.poll().observations) == 5

    log.write_text(json.dumps({"kind": "fresh", "ts": 9}) + "\n")   # rotated
    report = adapter.poll()
    assert report.available is True
    assert [o.event for o in report.observations] == ["fresh"]
    assert "rotated" in report.note


def test_jsonl_adapter_reports_missing_file_and_malformed_rows(tmp_path):
    absent = JsonlLogAdapter(tmp_path / "nope.jsonl").poll()
    assert absent.available is False and "not empty" in absent.note

    log = tmp_path / "mixed.jsonl"
    log.write_text(json.dumps({"kind": "ok", "ts": 1}) + "\n{bad json}\n[1,2]\n")
    report = JsonlLogAdapter(log).poll()
    assert report.available is True
    assert len(report.observations) == 1
    assert "malformed" in report.note        # surfaced, not silently dropped


def test_poll_all_isolates_a_failing_adapter(tmp_path):
    log = tmp_path / "ok.jsonl"
    log.write_text(json.dumps({"kind": "ok", "ts": 1}) + "\n")

    class Exploding:
        source = "boom"

        def poll(self, now=None):
            raise RuntimeError("kaboom")

    summary = poll_all([JsonlLogAdapter(log), Exploding(),
                        SystemdTimerAdapter(runner=lambda c: (0, TIMER_OUT, ""))],
                       now=5.0)
    assert summary["sources"] == 3
    assert summary["available"] == 2
    assert summary["unavailable"] == ["boom"]
    assert summary["observations"] == 3      # 1 jsonl + 2 timers
