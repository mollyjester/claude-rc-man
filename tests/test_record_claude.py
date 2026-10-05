"""The recorder: sanitising real screens into fixtures, and the fixture set itself."""

import json
import shutil
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


def test_session_ids_are_replaced():
    text = "registration failed for session cse_01SUPH9aMdURTQY7NjA7HeMM: Request failed"
    assert sanitise(text) == "registration failed for session {session_id}: Request failed"


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
        session_id="cse_01abc",
    )
    template = "{version} {cwd} {config_dir} {login_url} {environment_url} [{time}] {session_id}"
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


def leaks(path: Path) -> list[str]:
    """What the screen in ``path`` gives away that sanitising should have replaced, each
    found by the text that gives it away."""
    marks = {
        "an email address": ("@",),
        "a home or temporary path": ("/home/", "/tmp/"),
        "a sign-in URL": ("oauth/authorize?",),
        "an environment ID": ("environment=env_",),
        "a session ID": ("cse_",),
    }
    text = path.read_text(encoding="utf-8")
    return [leak for leak, found in marks.items() if any(mark in text for mark in found)]


@pytest.mark.parametrize("directory", [rec.FIXTURES, rec.OBSERVED], ids=lambda d: d.name)
def test_no_fixture_contains_an_unsanitised_secret_or_path(directory):
    paths = sorted(directory.glob("*.txt"))
    assert paths, f"no screens in {directory}"
    assert {path.name: leaks(path) for path in paths if leaks(path)} == {}


def test_the_leak_check_finds_a_session_id(tmp_path):
    """Planted twin: a screen still holding a session ID is caught."""
    planted = tmp_path / "planted.txt"
    planted.write_text("[10:00:00] failed for session cse_01ABCdef: 404", encoding="utf-8")
    assert leaks(planted) == ["a session ID"]
    clean = tmp_path / "clean.txt"
    clean.write_text(rec.sanitise(planted.read_text(encoding="utf-8"), CTX), encoding="utf-8")
    assert leaks(clean) == []


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


def test_any_email_address_on_a_screen_is_replaced():
    assert sanitise("Logged in as someone.else+x@sub.example.org.") == "Logged in as {email}."


def test_sequences_needing_a_person_never_run_unasked():
    human = {s.name for s in rec.SEQUENCES if s.needs_human}
    assert human, "no sequence needs a person"  # non-vacuity: the sign-in recording exists
    everything = {s.name for s in rec.select_sequences(None, with_account=True)}
    assert human.isdisjoint(everything)
    assert [s.name for s in rec.select_sequences(sorted(human), with_account=True)] == sorted(human)


# --- servers in a git repository --------------------------------------------------

GIT_CTX = rec.Context(cwd="/work/proj-x", config_dir="/cfg/dir", version="9.9.9", branch="topic")


def test_the_branch_in_the_status_line_is_replaced_in_a_repository():
    line = "·✔︎· Connected · proj-x · topic"
    assert rec.sanitise(line, GIT_CTX) == "·✔︎· Connected · {folder} · {branch}"


def test_the_branch_is_replaced_only_in_the_status_line():
    text = "topic\n·✔︎· Connected · proj-x · topic"
    assert rec.sanitise(text, GIT_CTX) == "topic\n·✔︎· Connected · {folder} · {branch}"


def test_outside_a_repository_the_status_line_keeps_what_claude_shows():
    line = "·✔︎· Connected · proj-x · HEAD"
    assert "{branch}" in rec.sanitise(line.replace("HEAD", "topic"), GIT_CTX)  # known positive
    assert sanitise(line) == "·✔︎· Connected · {folder} · HEAD"


def test_fill_puts_the_branch_back():
    values = rec.placeholder_values(GIT_CTX)
    assert values["branch"] == "topic"
    assert rec.fill("· {branch}", values) == "· topic"


def test_a_server_is_recorded_as_rc_helper_starts_one_with_spawn_mode_same_dir():
    assert rec.RC_ARGS[rec.RC_ARGS.index("--spawn") + 1] == "same-dir"
    assert "--spawn" in rec.options_used()[("remote-control",)]


def test_the_spawn_mode_question_is_recorded_without_spawn_in_a_repository():
    by_name = {s.name: s for s in rec.SEQUENCES}
    question = by_name["spawn_prompt"]
    assert question.git_repository
    assert "--spawn" not in question.args
    assert [a for a in question.args] == [
        a for a in rec.RC_ARGS if a not in ("--spawn", "same-dir")
    ]
    server = by_name["git_server"]
    assert server.git_repository and server.args == rec.RC_ARGS
    assert not by_name["server"].git_repository  # known negative


def test_a_repository_to_record_in_is_made_on_the_recorded_branch(tmp_path):
    import subprocess

    folder = tmp_path / "proj"
    folder.mkdir()
    rec.make_repository(folder)
    head = subprocess.run(
        ["git", "-C", str(folder), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == rec.RECORD_BRANCH
    commits = subprocess.run(
        ["git", "-C", str(folder), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert commits == "1"


# --- screens observed on a live server ---------------------------------------------


def unlisted_screens(directory: Path) -> list[str]:
    """The screens in ``directory`` its manifest does not list. (One it lists that is not
    there fails load_observed itself.)"""
    on_disk = {p.stem for p in directory.glob("*.txt")}
    return sorted(on_disk - set(rec.load_observed(directory)))


def test_the_observed_manifest_names_exactly_the_files_beside_it():
    assert list(rec.OBSERVED.glob("*.txt")), "no observed screens at all"
    assert unlisted_screens(rec.OBSERVED) == []


def observed_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "observed"
    shutil.copytree(rec.OBSERVED, copy)
    return copy


def test_a_screen_the_observed_manifest_does_not_list_is_found(tmp_path):
    """Planted twin: a screen kept beside the manifest but not listed in it."""
    copy = observed_copy(tmp_path)
    (copy / "planted_screen.txt").write_text("[{time}] planted", encoding="utf-8")
    assert unlisted_screens(copy) == ["planted_screen"]


def test_a_manifest_naming_a_file_that_is_not_there_is_refused(tmp_path):
    """Planted twin: the manifest is the reader's list, so a screen it names must exist."""
    copy = observed_copy(tmp_path)
    name = sorted(rec.load_observed(copy))[0]
    (copy / f"{name}.txt").unlink()
    with pytest.raises(FileNotFoundError):
        rec.load_observed(copy)


def not_kept_for(observed: dict, version: str) -> list[str]:
    """The observed screens whose manifest does not keep them for ``version``."""
    return sorted(name for name, screen in observed.items() if version not in screen.kept_for)


def test_every_observed_screen_is_kept_for_the_approved_version():
    """An observed screen cannot be re-recorded: approving a Claude Code version means
    recording that each is kept for it, or removing it."""
    observed = rec.load_observed()
    assert observed
    assert not_kept_for(observed, rec.load_manifest()["claude_version"]) == []
    for name, screen in observed.items():
        assert screen.kept_for[0] == screen.seen_on, name  # kept first for where it was seen


def test_an_observed_screen_not_kept_for_the_approved_version_is_found(tmp_path):
    """Planted twin: a manifest that does not name the approved version fails."""
    copy = observed_copy(tmp_path)
    manifest = json.loads((copy / rec.MANIFEST).read_text(encoding="utf-8"))
    name = sorted(manifest["screens"])[0]
    manifest["screens"][name]["kept_for"] = ["0.0.1"]
    (copy / rec.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    approved = rec.load_manifest()["claude_version"]
    assert approved != "0.0.1"
    assert not_kept_for(rec.load_observed(copy), approved) == [name]
