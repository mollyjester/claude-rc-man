"""rcman-user: the administrator's command line over SSH."""

import io

import pytest

from helpers import rc_admin as ra
from helpers import rcman_user as ru
from helpers import users_file as uf
from tests.test_rc_admin import FakeSystem, existing


@pytest.fixture
def cfg(tmp_path):
    return ra.Config(
        etc=tmp_path / "etc",
        user_group="rcusers",
        app_group="rcman",
        claude_version="9.9.9",
        installer=None,
    )


@pytest.fixture
def system(cfg, tmp_path):
    s = FakeSystem(
        tmp_path, {**existing(tmp_path, "alice", 2001), **existing(tmp_path, "bob", 2002)}
    )
    uf.save(
        cfg.users_file,
        [uf.User("alice@example.com", "alice", True), uf.User("bob@example.com", "bob", False)],
    )
    return s


class Tty(io.StringIO):
    def isatty(self):
        return True


def run(cfg, system, argv, stdin=None, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = ru.main(
        argv,
        cfg=cfg,
        system=system,
        stdin=stdin or io.StringIO(),
        stdout=out,
        stderr=err,
        env=env or {},
    )
    return code, out.getvalue(), err.getvalue()


def userdels(system):
    return [c for c in system.commands if c[0] == "userdel"]


def test_delete_flag_deletes_home_without_asking(cfg, system):
    code, out, _ = run(cfg, system, ["del", "-d", "bob"], env={"SUDO_USER": "alice"})
    assert code == 0
    assert userdels(system) == [["userdel", "--remove", "bob"]]


def test_keep_home_flag_keeps_it_without_asking(cfg, system):
    run(cfg, system, ["del", "--keep-home", "bob"], env={"SUDO_USER": "alice"})
    assert userdels(system) == [["userdel", "bob"]]


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("y\n", ["userdel", "--remove", "bob"]),
        ("\n", ["userdel", "bob"]),
        ("n\n", ["userdel", "bob"]),
    ],
)
def test_without_a_flag_it_asks_and_defaults_to_keeping(cfg, system, answer, expected):
    code, out, _ = run(cfg, system, ["del", "bob"], stdin=Tty(answer), env={"SUDO_USER": "alice"})
    assert code == 0
    assert "[y/N]" in out
    assert userdels(system) == [expected]


def test_without_a_flag_or_a_terminal_it_refuses(cfg, system):
    code, _, err = run(cfg, system, ["del", "bob"], env={"SUDO_USER": "alice"})
    assert code == 1
    assert ra.MESSAGES["home_decision_required"] in err
    assert userdels(system) == []


def test_the_sudo_user_cannot_delete_themselves(cfg, system):
    uf.save(
        cfg.users_file,
        [uf.User("alice@example.com", "alice", True), uf.User("bob@example.com", "bob", True)],
    )
    code, _, err = run(cfg, system, ["del", "-d", "bob"], env={"SUDO_USER": "bob"})
    assert code == 1 and ra.MESSAGES["cannot_delete_self"] in err


def test_an_invalid_linux_name_is_explained_by_its_rule(cfg, system):
    code, _, err = run(cfg, system, ["add", "Bad!", "dave@example.com"])
    assert code == 1
    assert err.strip() == uf.LINUX_NAME.sentence
    assert system.commands == []


def test_add_reports_in_words(cfg, system):
    code, out, _ = run(
        cfg, system, ["add", "carol", "carol@example.com"], env={"SUDO_USER": "alice"}
    )
    assert code == 0
    assert "carol" in out and "carol@example.com" in out


def test_list_prints_a_line_per_person(cfg, system):
    code, out, _ = run(cfg, system, ["list"])
    assert code == 0
    assert [line.split()[0] for line in out.strip().splitlines()[1:]] == ["alice", "bob"]


def test_usage_error_exits_2(cfg, system):
    code, _, _ = run(cfg, system, ["frobnicate"])
    assert code == 2


def test_claude_status_in_words(cfg, system):
    system.claude_versions.update({"alice": "9.9.9", "bob": "1.0.0"})
    code, out, _ = run(cfg, system, ["claude-status"])
    assert code == 0
    assert "approved 9.9.9" in out and "tested 9.9.9" in out
    assert "bob" in out and "1.0.0" in out and "differs" in out


def test_claude_upgrade_reports_each_user(cfg, system):
    system.claude_versions.update({"alice": "9.9.9", "bob": "9.9.9"})
    code, out, _ = run(cfg, system, ["claude-upgrade", "9.9.10"])
    assert code == 0
    assert "Approved Claude Code 9.9.10" in out
    assert "alice: installed" in out and "bob: installed" in out
    assert "newer than the tested 9.9.9" in out


def test_claude_upgrade_failure_names_the_checks(cfg, system):
    system.check_failures = {
        "version": {"lines": ["ran: claude --version", "planted version output"], "more": 0},
        "logout": {"lines": ["ran: claude auth logout", "planted logout output"], "more": 7},
    }
    code, _, err = run(cfg, system, ["claude-upgrade", "9.9.10"])
    assert code == 1
    assert "(failed: version, logout)" in err
    assert (
        "version:\n"
        "    ran: claude --version\n"
        "    planted version output\n"
        "logout:\n"
        "    ran: claude auth logout\n"
        "    planted logout output\n"
        "    (7 more lines not shown)\n"
    ) in err
    assert err.count("not shown") == 1  # only where lines were left out


def test_rolling_back_says_older(cfg, system):
    system.claude_versions.update({"alice": "9.9.9", "bob": "9.9.9"})
    code, out, _ = run(cfg, system, ["claude-upgrade", "9.9.1"])
    assert "older than the tested 9.9.9" in out
