"""The recorder: sanitising real screens into fixtures, and the fixture set itself."""

import json
import sys
from pathlib import Path

import pytest

from devtools import record_claude as rec
from devtools import tmuxctl

CTX = rec.Context(cwd="/work/proj-x", config_dir="/cfg/dir", version="9.9.9")


def sanitise(text: str) -> str:
    return rec.sanitise(text, CTX)


# --- sanitising: each placeholder ---------------------------------------------


def test_version_is_replaced():
    assert sanitise("9.9.9 (Claude Code)") == "{version} (Claude Code)"


def test_working_directory_is_replaced_everywhere():
    text = "  /work/proj-x\nTrust /work/proj-x? [y/N]"
    assert sanitise(text) == "  {cwd}\nTrust {cwd}? [y/N]"


def test_config_directory_is_replaced():
    assert sanitise('"configDirectory": "/cfg/dir"') == '"configDirectory": "{config_dir}"'
    assert sanitise('"projectsDirectory": "/cfg/dir/projects"') == (
        '"projectsDirectory": "{config_dir}/projects"'
    )


def test_folder_name_in_the_status_line_is_replaced():
    assert sanitise("·✔︎· Connected · proj-x · HEAD") == "·✔︎· Connected · {folder} · HEAD"


def test_login_url_is_replaced_whole():
    url = "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=s3cr3t"
    assert sanitise(f"visit: {url}") == "visit: {login_url}"


def test_environment_url_is_replaced_whole():
    url = "https://claude.ai/code?environment=env_01ABCdef"
    assert sanitise(f"or {url}") == "or {environment_url}"


def test_clock_times_are_replaced():
    assert sanitise("[14:01:45] Shutting down") == "[{time}] Shutting down"


def test_session_name_under_the_capacity_line_is_replaced():
    text = "    Capacity: 1/32 · New sessions\n    rcprobe-elegant-magpie\n"
    assert sanitise(text) == "    Capacity: 1/32 · New sessions\n    {session_name}\n"


def test_no_session_name_line_is_left_alone():
    text = "    Capacity: 1/32 · New sessions\n\nContinue coding"
    assert sanitise(text) == text


def test_account_fields_are_replaced():
    text = (
        '  "email": "someone@example.com",\n'
        '  "orgId": "71664fa6-0000",\n'
        '  "orgName": "Some Org",\n'
        '  "subscriptionType": "team"\n'
    )
    assert sanitise(text) == (
        '  "email": "{email}",\n'
        '  "orgId": "{org_id}",\n'
        '  "orgName": "{org_name}",\n'
        '  "subscriptionType": "{subscription_type}"\n'
    )


def test_fill_is_the_inverse_of_sanitise():
    values = rec.placeholder_values(
        CTX,
        login_url="https://claude.com/cai/oauth/authorize?x",
        environment_url="https://claude.ai/code?environment=env_1",
        time="01:02:03",
        session_name="p-a-b",
        email="e@example.com",
        org_id="o",
        org_name="n",
        subscription_type="t",
    )
    template = "{version} {cwd} {config_dir} {login_url} {environment_url} [{time}]"
    assert sanitise(rec.fill(template, values)) == template


def test_fill_rejects_an_unknown_placeholder():
    with pytest.raises(KeyError):
        rec.fill("{nonsense}", {})


# --- the fixture set ------------------------------------------------------------


def test_manifest_and_fixture_files_agree_both_ways():
    manifest = rec.load_manifest()
    on_disk = {p.stem for p in rec.FIXTURES.glob("*.txt")}
    assert on_disk, "no fixture files at all"
    assert set(manifest["captures"]) == on_disk


def test_every_capture_belongs_to_a_defined_sequence_and_vice_versa():
    manifest = rec.load_manifest()
    defined = {c for seq in rec.SEQUENCES for c in seq.captures}
    assert set(manifest["captures"]) == defined
    for name, entry in manifest["captures"].items():
        assert entry["sequence"] in {s.name for s in rec.SEQUENCES}, name


def test_manifest_records_one_claude_version():
    manifest = rec.load_manifest()
    assert rec.VERSION_PATTERN.fullmatch(manifest["claude_version"])


def test_no_fixture_contains_an_unsanitised_secret_or_path():
    for path in rec.FIXTURES.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        assert "@" not in text, path.name  # emails
        assert "/home/" not in text and "/tmp/" not in text, path.name
        assert "oauth/authorize?" not in text, path.name
        assert "environment=env_" not in text, path.name


def test_options_used_are_derived_from_the_sequences():
    used = rec.options_used()
    # Known positives: rcman relies on these.
    assert "--remote-control-session-name-prefix" in used[("remote-control",)]
    assert "--email" in used[("auth", "login")]


def test_compare_reports_a_mismatch(tmp_path):
    """Planted twin for the comparison every contract and round-trip test uses."""
    recorded = {"version": rec.Recorded(lines=["{version} (Claude Code)", "extra"], exit_code=0)}
    expected = {"version": rec.Recorded(lines=["{version} (Claude Code)"], exit_code=0)}
    assert rec.compare(recorded, expected) != []
    assert rec.compare(expected, expected) == []


def test_compare_reports_an_exit_code_mismatch():
    a = {"x": rec.Recorded(lines=["same"], exit_code=0)}
    b = {"x": rec.Recorded(lines=["same"], exit_code=1)}
    assert rec.compare(a, b) != []


def test_write_then_load_round_trips(tmp_path):
    recorded = {"version": rec.Recorded(lines=["{version} (Claude Code)"], exit_code=0)}
    rec.write_fixtures(recorded, version="1.2.3", out=tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["claude_version"] == "1.2.3"
    assert rec.load_fixtures(tmp_path) == recorded


def test_a_capture_whose_screen_changes_while_settling_is_refused(tmp_path):
    """The screen must still match when it has settled, or the capture is mislabelled."""
    code = (
        "import time\n"
        "print('stage-one', flush=True); time.sleep(0.5)\n"
        "print('\\x1b[H\\x1b[2Jstage-two', flush=True); time.sleep(30)\n"
    )
    with tmuxctl.Pane(socket_dir=tmp_path) as pane:
        argv = [sys.executable, "-c", code]
        pane.run(argv, cwd=tmp_path, env={})
        run = rec.Run(pane, argv, Path(tmp_path), {}, CTX)
        with pytest.raises(RuntimeError, match="stage-one"):
            run.capture_when(r"stage-one", "planted")
    assert "planted" not in run.out


def test_sequences_can_be_selected_by_name():
    chosen = rec.select_sequences(["server"], with_account=True)
    assert [s.name for s in chosen] == ["server"]
    with pytest.raises(ValueError):
        rec.select_sequences(["no-such-sequence"], with_account=True)
    with pytest.raises(ValueError, match="--with-account"):
        rec.select_sequences(["server"], with_account=False)


def test_help_refused_logged_out_recognises_the_recorded_refusal():
    refusal = "\n".join(rec.load_fixtures()["rc_logged_out"].lines)
    assert rec.help_refused_logged_out(refusal)
    assert not rec.help_refused_logged_out("Usage: claude remote-control [options]")
