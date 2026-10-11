from types import SimpleNamespace

from scripts import verify_advanced_agent as verifier


def test_verifier_preserves_failure_and_summary_when_both_streams_are_large(monkeypatch):
    completed = SimpleNamespace(
        returncode=1,
        stdout="browser progress\n" * 2000 + "Error: expected delete confirmation\n135 passed, 1 failed in 10.00s\n",
        stderr="server request\n" * 2000 + "server stopped\n",
    )
    monkeypatch.setattr(verifier.subprocess, "run", lambda *_args, **_kwargs: completed)

    result = verifier.command("browser-integration", ["repository-test-command"])

    assert result.status == "FAIL"
    assert "Error: expected delete confirmation" in result.output
    assert "135 passed, 1 failed" in result.detail
    assert "server stopped" in result.output
    assert len(result.output) <= 20_000


def test_verifier_cannot_hide_skipped_tests_behind_verbose_stderr(monkeypatch):
    completed = SimpleNamespace(
        returncode=0,
        stdout="1 passed, 1 skipped in 0.01s\n",
        stderr="server request\n" * 2000 + "server stopped\n",
    )
    monkeypatch.setattr(verifier.subprocess, "run", lambda *_args, **_kwargs: completed)

    result = verifier.command_no_skips("database-integration", ["repository-test-command"])

    assert result.status == "BLOCKED"
    assert "必需验收用例被跳过" in result.detail
    assert completed.stdout in result.output
    assert "server stopped" in result.output
    assert len(result.output) <= 20_000
