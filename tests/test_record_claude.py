"""The recorder: sanitising real screens into fixtures, and the fixture set itself."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from devtools import held_folders, tmuxctl
from devtools import record_claude as rec
from tests import killed_runs as kr

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


# What a screen can give away that sanitising should have replaced: each kind, with the
# marks, the text that gives it away.
LEAK_MARKS = {
    "an email address": ("@",),
    "a home or temporary path": ("/home/", "/tmp/"),
    "a sign-in URL": ("oauth/authorize?",),
    "an environment ID": ("environment=env_",),
    "a session ID": ("cse_",),
}
# Planted screen lines, each giving away what one mark finds and nothing else.
LEAK_SAMPLES = (
    "Logged in as alice@example.com",
    "cwd: /home/alice/project",
    f"cwd: {held_folders.TMP}/claude-1001/scratch",  # built: no fixed path here
    "Open https://claude.ai/oauth/authorize?code=true&client_id=x",
    "https://claude.ai/code?environment=env_01ABCdef",
    "[10:00:00] failed for session cse_01ABCdef: 404",
)


def sample_of(mark: str) -> str:
    """The one planted sample that holds ``mark``."""
    [sample] = [sample for sample in LEAK_SAMPLES if mark in sample]
    return sample


def leaks(path: Path) -> list[str]:
    """What the screen in ``path`` gives away that sanitising should have replaced, each
    found by the text that gives it away."""
    text = path.read_text(encoding="utf-8")
    return [leak for leak, found in LEAK_MARKS.items() if any(mark in text for mark in found)]


@pytest.mark.parametrize("directory", [rec.FIXTURES, rec.OBSERVED], ids=lambda d: d.name)
def test_no_fixture_contains_an_unsanitised_secret_or_path(directory):
    paths = sorted(directory.glob("*.txt"))
    assert paths, f"no screens in {directory}"
    assert {path.name: leaks(path) for path in paths if leaks(path)} == {}


def test_a_sanitised_session_id_is_no_leak(tmp_path):
    """The session ID sample, a leak as it is, is none once sanitised."""
    sample = sample_of("cse_")
    clean = tmp_path / "clean.txt"
    clean.write_text(rec.sanitise(sample, CTX), encoding="utf-8")
    assert clean.read_text(encoding="utf-8") != sample  # sanitising replaced something
    assert leaks(clean) == []


def test_every_leak_mark_has_a_planted_sample():
    marks = [mark for kinds in LEAK_MARKS.values() for mark in kinds]
    held = [[mark for mark in marks if mark in sample] for sample in LEAK_SAMPLES]
    assert sorted(mark for found in held for mark in found) == sorted(marks)
    assert all(len(found) == 1 for found in held), held  # one mark to a sample


@pytest.mark.parametrize(
    ("kind", "mark"),
    [(kind, mark) for kind, marks in LEAK_MARKS.items() for mark in marks],
    ids=lambda value: value,
)
def test_the_leak_check_finds_each_mark_as_its_kind(tmp_path, kind, mark):
    """Planted twins: a screen holding only one mark's sample is caught as that mark's
    kind, and as nothing else."""
    planted = tmp_path / "planted.txt"
    planted.write_text(sample_of(mark), encoding="utf-8")
    assert leaks(planted) == [kind]


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
    with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as pane:
        argv = [sys.executable, "-c", code]
        pane.run(argv, cwd=tmp_path, env={})
        folder = pane.socket.parent  # held, so a killed run leaves it for the next to stop
        assert held_folders.made_with("rcp-")(folder.name) and not held_folders.unheld(folder)
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


def test_a_server_is_recorded_as_rcman_starts_one_with_spawn_mode_same_dir():
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
    approved = rec.load_manifest()["claude_version"]
    assert not_kept_for(rec.load_observed(copy), approved) == []  # positive control: none yet
    manifest = json.loads((copy / rec.MANIFEST).read_text(encoding="utf-8"))
    name = sorted(manifest["screens"])[0]
    manifest["screens"][name]["kept_for"] = ["0.0.1"]
    (copy / rec.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    assert approved != "0.0.1"
    assert not_kept_for(rec.load_observed(copy), approved) == [name]


# --- the recorder's folders in /tmp: held, and cleared after a killed run -------------


def _held(folder: Path, prefix: str) -> bool:
    return (
        held_folders.made_with(prefix)(folder.name)
        and folder.parent == held_folders.TMP
        and (folder / held_folders.LOCK).is_file()
        and not held_folders.unheld(folder)
    )


READS_THE_VERSION = """
import os, shlex
from pathlib import Path
from devtools import record_claude as rec
from tests import killed_runs as kr

rec.VERSIONS = {prefix!r}

def test_reads(tmp_path):
    fake = tmp_path / "claude"
    reports = Path(os.environ[kr.ENV_REPORTS])
    fake.write_text(
        "#!/bin/sh\\n"
        + kr.shell_reporter(reports)
        + 'report version "{{\\\\"paths\\\\": {{\\\\"folder\\\\": \\\\"$PWD\\\\"}}, '
        + '\\\\"pids\\\\": {{\\\\"tmux\\\\": $PPID}}}}"\\n'
        + kr.shell_hold()
        + "echo 1.2.3\\n"
    )
    fake.chmod(0o755)
    rec.installed_version(fake)
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["SIGTERM", "SIGKILL"])
def test_the_next_run_clears_what_a_killed_version_reading_left(tmp_path, sig):
    with kr.stem() as stem:
        prefix = stem + rec.VERSIONS
        source = {"test_version.py": READS_THE_VERSION.format(prefix=prefix)}
        with kr.KilledRun(tmp_path, source, (stem,)) as run:
            reported = run.reports.wait("version")
            run.kill(sig)
        folder, pid = Path(reported["paths"]["folder"]), reported["pids"]["tmux"]
        assert held_folders.made_with(prefix)(folder.name)
        assert folder.is_dir() and kr.running(pid)  # positive control: the kill left them
        with held_folders.held(prefix):
            assert not os.path.lexists(folder) and not kr.running(pid)


def test_reading_the_version_happens_in_a_held_folder(monkeypatch):
    seen = []

    class Pane:
        def __init__(self, socket_dir):
            seen.append(socket_dir)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def run(self, argv, cwd, env):
            seen.append(_held(cwd, rec.VERSIONS) and cwd == seen[0])

        def wait_exit(self, timeout):
            return 0

        def text(self):
            return "1.2.3 (Claude Code)"

    monkeypatch.setattr(rec.tmuxctl, "Pane", Pane)
    assert rec.installed_version("claude") == "1.2.3"
    assert seen[1:] == [True] and not seen[0].exists()


def test_help_runs_in_a_held_folder_that_is_removed_afterwards(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\necho "$PWD"\nls -a\n')
    fake.chmod(0o755)
    env = rec.claude_env(None, tmp_path / "home")
    text = rec.help_text(fake, ("remote-control",), env).splitlines()
    folder = Path(text[0])
    assert held_folders.made_with(rec.HELP)(folder.name) and held_folders.LOCK in text
    assert not folder.exists()


def test_each_sequence_runs_its_tmux_server_in_a_held_folder(tmp_path, monkeypatch):
    seen = []

    class Pane:
        def __init__(self, socket_dir):
            seen.append((socket_dir, _held(socket_dir, rec.PANES)))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def run(self, argv, cwd, env):
            pass

    monkeypatch.setattr(rec, "installed_version", lambda claude: "1.2.3")
    monkeypatch.setattr(rec.tmuxctl, "Pane", Pane)
    sequence = rec.Sequence("probe", ["--version"], (), lambda run: None)
    rec.record("claude", [sequence], None, tmp_path / "work")
    assert [held for _, held in seen] == [True]
    assert not seen[0][0].exists()


def test_a_recording_works_in_a_held_folder(tmp_path, monkeypatch):
    seen = []

    def record(claude, sequences, account_config_dir, workdir):
        seen.append(_held(workdir, rec.WORK))
        return {}

    monkeypatch.setattr(rec, "installed_version", lambda claude: "1.2.3")
    monkeypatch.setattr(rec, "record", record)
    monkeypatch.setattr(rec, "write_fixtures", lambda *args, **kwargs: None)
    assert rec.main(["--claude", "claude", "--out", str(tmp_path)]) == 0
    assert seen == [True]


def test_a_sign_in_hands_its_code_over_in_the_recordings_working_folder(
    tmp_path, capsys, monkeypatch
):
    """Nothing fixed in /tmp: a handoff folder of its own, named on stderr."""
    url = rec.LOGIN_URL_PREFIX + "x=1"
    typed = []

    class Pane:
        def wait_for(self, pattern, timeout):
            return re.search(pattern, url)

        def type_line(self, text):
            typed.append(text)

        def rerun(self, *args):
            pass

        def wait_exit(self, timeout):
            return 0

    class Run:
        pane = Pane()
        argv = ["claude"]
        cwd = tmp_path / "work" / "login_succeeded-x"
        env = {}

        def capture_when(self, pattern, name):
            pass

        def capture_exit(self, name):
            pass

    Run.cwd.mkdir(parents=True)
    written, write = [], Path.write_text

    def writing(path, *args, **kwargs):
        written.append(path.name)
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", writing)
    signing_in = threading.Thread(target=rec._login_succeeded, args=(Run(),), daemon=True)
    signing_in.start()
    deadline = time.monotonic() + 30
    while not list((tmp_path / "work").glob("handoff-*/url")):
        assert time.monotonic() < deadline and signing_in.is_alive()
        time.sleep(0.05)
    (handoff,) = (tmp_path / "work").glob("handoff-*")
    assert (handoff / "url").read_text() == url + "\n"
    # The person writes the code in place: the line is read only once it is ended.
    with open(handoff / "code", "w") as code:
        code.write("THE-")
        code.flush()
        time.sleep(3 * rec.HANDOFF_POLL)
        assert typed == [] and signing_in.is_alive()  # half a code is not typed
        code.write("CODE\n")
    signing_in.join(timeout=30)
    assert not signing_in.is_alive()
    assert typed[0] == "THE-CODE"
    assert str(handoff) in capsys.readouterr().err
    assert "url" not in written  # written elsewhere and renamed, so never read in part


# --- the home a recorded claude runs with --------------------------------------------------

# A claude that leaves a mark in its home.
MARKING = '#!/bin/sh\ntouch "$HOME/marked"\n'


def test_claude_env_gives_home_a_folder_of_its_own_never_the_real_one(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    env = rec.claude_env(None, tmp_path / "home")
    assert env["HOME"] == str(tmp_path / "home") and "CLAUDE_CONFIG_DIR" not in env
    assert rec.claude_env(tmp_path / "cfg", tmp_path / "home")["CLAUDE_CONFIG_DIR"] == str(
        tmp_path / "cfg"
    )
    with pytest.raises(ValueError):
        rec.claude_env(None, tmp_path / "real-home")  # the home the run was started from


def recorded_with(fake: Path, workdir: Path, needs_account: bool) -> None:
    sequence = rec.Sequence(
        "probe", [], ("probe",), lambda run: run.capture_exit("probe"), needs_account=needs_account
    )
    rec.record(fake, [sequence], None, workdir)


def told(tmp_path: Path) -> Path:
    """A claude that writes down the environment it was given: HOME, CLAUDE_CONFIG_DIR, and
    where HOME's .claude and .claude.json lead; and leaves a mark in HOME."""
    seen = tmp_path / "seen"
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        f'{{ echo "home=$HOME"; echo "config=${{CLAUDE_CONFIG_DIR-unset}}"; '
        'echo "claude=$(readlink "$HOME/.claude")"; '
        f'echo "json=$(readlink "$HOME/.claude.json")"; }} > {seen}\n'
        'touch "$HOME/marked"\n'
    )
    fake.chmod(0o755)
    return fake


def seen_by(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "seen").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


@pytest.fixture
def real_home(tmp_path, monkeypatch) -> Path:
    """The home the run was started from, with an account in it as Claude keeps one: its
    folder, and its .claude.json beside the folder, not in it."""
    home = tmp_path / "real-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(rec, "installed_version", lambda claude: "1.2.3")
    return home


def test_a_recording_without_the_account_runs_in_a_home_and_a_folder_of_its_own(
    tmp_path, real_home
):
    """``account_config_dir=None``, as tests/test_claude_contract.py records the screens of no
    account: a configuration folder of its own, and a home of its own that leads nowhere."""
    recorded_with(told(tmp_path), tmp_path / "work", needs_account=False)
    seen = seen_by(tmp_path)
    assert seen["home"] != str(real_home) and Path(seen["home"]).is_relative_to(tmp_path / "work")
    assert seen["config"] != "unset" and Path(seen["config"]).is_relative_to(tmp_path / "work")
    assert (seen["claude"], seen["json"]) == ("", "")
    assert not (real_home / "marked").exists() and (Path(seen["home"]) / "marked").exists()


def account_problems(seen: dict[str, str], real_home: Path, work: Path) -> list[str]:
    """What is wrong with the environment an account's sequence was given: Claude reads the
    account's .claude.json from $HOME when no CLAUDE_CONFIG_DIR is set, and from that folder
    when one is, where the account has none."""
    problems = []
    if seen["config"] != "unset":
        problems.append(f"CLAUDE_CONFIG_DIR is {seen['config']}")
    if seen["home"] == str(real_home) or not Path(seen["home"]).is_relative_to(work):
        problems.append(f"HOME is {seen['home']}")
    if seen["claude"] != str(real_home / ".claude"):
        problems.append(f".claude leads to {seen['claude']!r}")
    if seen["json"] != str(real_home / ".claude.json"):
        problems.append(f".claude.json leads to {seen['json']!r}")
    return problems


def test_the_accounts_sequence_reads_the_account_where_claude_keeps_it(tmp_path, real_home):
    """With no configuration folder given, the account's: no CLAUDE_CONFIG_DIR, so Claude reads
    $HOME/.claude.json and $HOME/.claude, which lead to the account's own, in a home of its own
    that gets anything else Claude writes."""
    recorded_with(told(tmp_path), tmp_path / "work", needs_account=True)
    seen = seen_by(tmp_path)
    assert account_problems(seen, real_home, tmp_path / "work") == []
    assert not (real_home / "marked").exists() and (Path(seen["home"]) / "marked").exists()


def test_a_recorder_that_names_the_accounts_folder_is_found(tmp_path, real_home, monkeypatch):
    """Planted twin: the recorder giving the account's folder as CLAUDE_CONFIG_DIR, in a home
    with no links, where Claude finds no .claude.json; the check above finds it."""
    named = real_home / ".claude"
    real = rec.claude_env
    monkeypatch.setattr(rec, "account_home", lambda home: home)  # no links
    monkeypatch.setattr(rec, "claude_env", lambda config, home: real(config or named, home))
    recorded_with(told(tmp_path), tmp_path / "work", needs_account=True)
    seen = seen_by(tmp_path)
    assert seen["config"] == str(named)  # the plant took effect
    assert not (named / ".claude.json").exists()  # where Claude would then look
    problems = account_problems(seen, real_home, tmp_path / "work")
    assert problems[0] == f"CLAUDE_CONFIG_DIR is {named}" and len(problems) == 3


def test_an_account_home_leads_to_the_accounts_files(tmp_path, real_home):
    home = rec.account_home(tmp_path / "home")
    assert os.readlink(home / ".claude") == str(real_home / ".claude")
    assert os.readlink(home / ".claude.json") == str(real_home / ".claude.json")
    assert rec.claude_env(None, home)["HOME"] == str(home)


def test_a_claude_run_with_the_real_home_marks_it(tmp_path, monkeypatch):
    """Planted twin: the same claude given the home the run was started from marks it, so the
    check above would see a recording that did."""
    home = tmp_path / "real-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    fake = tmp_path / "claude"
    fake.write_text(MARKING)
    fake.chmod(0o755)
    env = {**rec.claude_env(None, tmp_path / "home"), "HOME": str(home)}
    subprocess.run([str(fake)], env=env, check=True, capture_output=True)
    assert (home / "marked").exists()


# --- the account a recording runs with, where the run names one -----------------------------


def test_the_accounts_folder_is_the_one_claude_is_configured_with(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert rec.account_config_dir() is None  # known negative: no folder is named
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    assert rec.account_config_dir() is None
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "account"))
    assert rec.account_config_dir() == tmp_path / "account"


@pytest.mark.parametrize("named", [True, False])
def test_a_full_recording_runs_in_the_accounts_folder_where_one_is_named(
    tmp_path, monkeypatch, named
):
    """The account of a run started with CLAUDE_CONFIG_DIR is in that folder, not in
    $HOME/.claude, so the recording is given it."""
    given = []

    def record(claude, sequences, account_config_dir, workdir):
        given.append(account_config_dir)
        return {}

    monkeypatch.setattr(rec, "installed_version", lambda claude: "1.2.3")
    monkeypatch.setattr(rec, "record", record)
    monkeypatch.setattr(rec, "write_fixtures", lambda *args, **kwargs: None)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    if named:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "account"))
    assert rec.main(["--claude", "claude", "--out", str(tmp_path)]) == 0
    assert given == [tmp_path / "account" if named else None]


def test_the_accounts_sequence_reads_the_account_in_a_named_folder(tmp_path, real_home):
    """With the account's folder named, Claude is given that folder, which holds its
    .claude.json, in a home of its own that leads nowhere and gets anything else it writes."""
    account = tmp_path / "account"
    account.mkdir()
    sequence = rec.Sequence(
        "probe", [], ("probe",), lambda run: run.capture_exit("probe"), needs_account=True
    )
    rec.record(told(tmp_path), [sequence], account, tmp_path / "work")
    seen = seen_by(tmp_path)
    assert seen["config"] == str(account)
    assert seen["home"] != str(real_home) and Path(seen["home"]).is_relative_to(tmp_path / "work")
    assert (seen["claude"], seen["json"]) == ("", "")
    assert not (real_home / "marked").exists() and (Path(seen["home"]) / "marked").exists()


@pytest.mark.parametrize("named", [True, False])
def test_the_environment_of_the_account_is_where_the_run_names_it(
    tmp_path, real_home, monkeypatch, named
):
    """What the real-account tests give ``claude``: the account's folder where the run
    names one, in a home of its own that leads nowhere; otherwise the links to the account
    in the home of the run, and no folder."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    if named:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "account"))
    home = tmp_path / "home"
    env = rec.account_env(home)
    assert env["HOME"] == str(home)
    if named:
        assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "account")
        assert not (home / ".claude").exists() and not (home / ".claude.json").exists()
    else:
        assert "CLAUDE_CONFIG_DIR" not in env
        assert os.readlink(home / ".claude") == str(real_home / ".claude")
        assert os.readlink(home / ".claude.json") == str(real_home / ".claude.json")
