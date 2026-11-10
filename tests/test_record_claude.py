"""The recorder: sanitising real screens into fixtures, the home it runs `claude` in, and
the fixture set itself."""

import ast
import json
import re
import shutil
from pathlib import Path

import pytest
from rcman import claude_versions as cv
from rcman.runtime import HELPERS_DIR

from devtools import held_folders
from devtools import record_claude as rec

CTX = rec.Context(cwd="/work/proj-x", config_dir="/cfg/dir", version="9.9.9", branch="topic")


# --- sanitising -----------------------------------------------------------------


def test_fill_is_the_inverse_of_sanitise_for_every_placeholder():
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
    template = (
        "{version} {cwd} {config_dir} {login_url} {environment_url} [{time}] {session_id}\n"
        '"email": "{email}", "orgId": "{org_id}", "orgName": "{org_name}", '
        '"subscriptionType": "{subscription_type}"\n'
        "    Capacity: 1/32 · New sessions\n"
        "    {session_name}\n"
        "·✔︎· Connected · {folder} · {branch}"
    )
    assert set(re.findall(r"\{(\w+)\}", template)) == set(rec.PLACEHOLDERS)  # every one
    filled = rec.fill(template, values)
    assert "{" not in filled  # the plant took effect: no placeholder is left
    assert rec.sanitise(filled, CTX) == template


def test_write_then_load_round_trips(tmp_path):
    recorded = {"version": rec.Recorded(lines=["{version} (Claude Code)"], exit_code=0)}
    rec.write_fixtures(recorded, version="1.2.3", out=tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["claude_version"] == "1.2.3"
    assert rec.load_fixtures(tmp_path) == recorded


# --- the fixture set ------------------------------------------------------------


def fixture_set_problems(directory: Path) -> list[str]:
    """The captures that are not each in the manifest, on disk and in a defined sequence."""
    captures = rec.load_manifest(directory)["captures"]
    on_disk = {p.stem for p in directory.glob("*.txt")}
    defined = {c for seq in rec.SEQUENCES for c in seq.captures}
    sequences = {s.name for s in rec.SEQUENCES}
    stray = {name for name, entry in captures.items() if entry["sequence"] not in sequences}
    return sorted((set(captures) ^ on_disk) | (set(captures) ^ defined) | stray)


def test_the_manifest_the_fixture_files_and_the_sequences_agree_both_ways():
    assert list(rec.FIXTURES.glob("*.txt")), "no fixture files at all"
    assert fixture_set_problems(rec.FIXTURES) == []


def test_a_fixture_file_the_manifest_does_not_list_is_found(tmp_path):
    """Planted twin: a fixture kept beside the manifest but not listed in it."""
    copy = tmp_path / "fixtures"
    shutil.copytree(rec.FIXTURES, copy)
    (copy / "planted_capture.txt").write_text("planted", encoding="utf-8")
    assert fixture_set_problems(copy) == ["planted_capture"]


# The tested version, the one rcman reads from TESTED_FILE in its helpers' folder (the Claude
# Code versions page's "Tested version"), and the version the fixtures were recorded from must be
# one: what is wrong when they are not, or None.
def version_mismatch(helpers: Path, fixtures: Path) -> str | None:
    tested_file = helpers / cv.TESTED_FILE
    manifest_file = fixtures / rec.MANIFEST
    tested = tested_file.read_text(encoding="utf-8").strip()
    recorded = rec.load_manifest(fixtures)["claude_version"]
    if tested == recorded:
        return None
    return f"{tested_file} names {tested}, but {manifest_file} records {recorded}"


def test_the_tested_version_is_the_one_the_fixtures_were_recorded_from():
    tested = (HELPERS_DIR / cv.TESTED_FILE).read_text(encoding="utf-8").strip()
    recorded = rec.load_manifest(rec.FIXTURES)["claude_version"]
    # positive control: a version was read from each
    assert rec.VERSION_PATTERN.fullmatch(tested)
    assert rec.VERSION_PATTERN.fullmatch(recorded)
    assert version_mismatch(HELPERS_DIR, rec.FIXTURES) is None


def plant_versions(root: Path, tested: str, recorded: str) -> tuple[Path, Path]:
    helpers, fixtures = root / "helpers", root / "fixtures"
    helpers.mkdir()
    fixtures.mkdir()
    (helpers / cv.TESTED_FILE).write_text(f"{tested}\n", encoding="utf-8")
    manifest = json.dumps({"claude_version": recorded})
    (fixtures / rec.MANIFEST).write_text(manifest, encoding="utf-8")
    return helpers, fixtures


def test_a_tested_version_the_fixtures_were_not_recorded_from_is_found(tmp_path):
    helpers, fixtures = plant_versions(tmp_path, "9.9.9", "1.2.3")
    # the plant took effect
    assert (helpers / cv.TESTED_FILE).read_text(encoding="utf-8").strip() == "9.9.9"
    assert rec.load_manifest(fixtures)["claude_version"] == "1.2.3"
    problem = version_mismatch(helpers, fixtures)
    assert problem is not None
    for named in ("9.9.9", "1.2.3", str(helpers / cv.TESTED_FILE), str(fixtures / rec.MANIFEST)):
        assert named in problem


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


def test_no_fixture_or_observed_screen_contains_an_unsanitised_secret_or_path():
    for directory in (rec.FIXTURES, rec.OBSERVED):
        paths = sorted(directory.glob("*.txt"))
        assert paths, f"no screens in {directory}"
        assert {path.name: leaks(path) for path in paths if leaks(path)} == {}


def test_the_leak_check_finds_each_mark_as_its_kind(tmp_path):
    """Planted twins: a screen holding only one mark's sample is caught as that mark's
    kind, and as nothing else."""
    planted = tmp_path / "planted.txt"
    for kind, marks in LEAK_MARKS.items():
        for mark in marks:
            planted.write_text(sample_of(mark), encoding="utf-8")
            assert leaks(planted) == [kind], mark


def sample_problems(table: dict[str, tuple[str, ...]], samples: tuple[str, ...]) -> list[str]:
    """The marks no sample holds, and the samples that hold other than one mark."""
    marks = [mark for kinds in table.values() for mark in kinds]
    problems = [f"{mark!r} has no sample" for mark in marks if not any(mark in s for s in samples)]
    for sample in samples:
        if len(held := [mark for mark in marks if mark in sample]) != 1:
            problems.append(f"{sample!r} holds {held}")
    return problems


def test_every_leak_mark_has_a_planted_sample_of_its_own():
    assert sample_problems(LEAK_MARKS, LEAK_SAMPLES) == []


def test_a_leak_mark_with_no_planted_sample_is_found():
    """Planted twin: a mark no sample holds."""
    planted = {**LEAK_MARKS, "a planted kind": ("PLANTED-MARK",)}
    assert sample_problems(planted, LEAK_SAMPLES) == ["'PLANTED-MARK' has no sample"]


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


# --- the home a recorded claude runs with --------------------------------------------------


def test_claude_env_gives_home_a_folder_of_its_own_never_the_real_one(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    env = rec.claude_env(None, tmp_path / "home")
    assert env["HOME"] == str(tmp_path / "home") and "CLAUDE_CONFIG_DIR" not in env
    assert rec.claude_env(tmp_path / "cfg", tmp_path / "home")["CLAUDE_CONFIG_DIR"] == str(
        tmp_path / "cfg"
    )
    with pytest.raises(ValueError):
        rec.claude_env(None, tmp_path / "real-home")  # the home the run was started from


# --- no test runs the command line's watch ---------------------------------------------------

TESTS = Path(__file__).resolve().parent


def reaches_real_watch(source: str) -> list[str]:
    """Where a test module would run RealWatch, the command line's watch, which waits on real
    time: a RealWatch made, or the recorder's main run, in a function that does not put a
    stand-in in place of ``record`` first. A module marked real_claude, run by hand against the
    real `claude` as the command line is, may."""
    tree = ast.parse(source)
    if re.search(r"^pytestmark = pytest\.mark\.real_claude$", source, re.M):
        return []
    found = []
    for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
        patched = any(
            isinstance(c.func, ast.Attribute) and c.func.attr == "setattr"
            and len(c.args) > 1 and isinstance(c.args[1], ast.Constant)
            and c.args[1].value == "record"
            for c in calls
        )  # fmt: skip
        for call in calls:
            name = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
            on = getattr(getattr(call.func, "value", None), "id", None)
            if name == "RealWatch":
                found.append(f"{function.name}:{call.lineno}: makes a RealWatch")
            elif name == "main" and on in ("rec", "record_claude") and not patched:
                found.append(f"{function.name}:{call.lineno}: runs main with record unpatched")
    return found


def test_no_test_reaches_the_command_lines_watch():
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(TESTS.rglob("*.py"))}
    assert "test_record_claude.py" in sources  # non-vacuity: the tests are read, this one too
    assert {
        name: found for name, src in sources.items() if (found := reaches_real_watch(src))
    } == {}


def test_a_test_that_reaches_the_command_lines_watch_is_found():
    """Planted twin: a RealWatch made, and the recorder's main run with ``record`` as it is."""
    made = "def test_x(tmp_path):\n    rec.record('claude', [], None, tmp_path, rec.RealWatch())\n"
    unpatched = "def test_x(tmp_path):\n    rec.main(['--out', str(tmp_path)])\n"
    assert len(reaches_real_watch(made)) == 1
    assert len(reaches_real_watch(unpatched)) == 1
