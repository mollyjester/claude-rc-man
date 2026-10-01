"""rc-admin decisions, against a recording stand-in for the system.

What these tests prove is *which* commands rc-admin decides to run and that a
refusal runs none. Whether those commands do the right thing on a real system
is proven in the container suite (tests/container/).
"""

import json
from pathlib import Path

import pytest

from helpers import rc_admin as ra
from helpers import users_file as uf

REPO = Path(__file__).resolve().parent.parent


class FakeSystem:
    """Users and groups in memory; every command recorded, none run."""

    def __init__(self, tmp_path: Path, users: dict | None = None):
        self.root = tmp_path
        self.users = users or {}
        self.commands: list[list[str]] = []
        self.as_user_commands: list[tuple[str, list[str]]] = []
        self.claude_versions: dict[str, str | None] = {}

    def user(self, name):
        info = self.users.get(name)
        return None if info is None else ra.UserInfo(name=name, **info)

    def group_gid(self, name):
        return None  # group ownership needs root; the container suite checks it

    def run(self, argv, check=True):
        self.commands.append(list(argv))
        if argv[0] == "useradd":
            name = argv[-1]
            home = self.root / "home" / name
            home.mkdir(parents=True)
            self.users[name] = {
                "uid": 2000 + len(self.users),
                "gid": 3000,
                "home": home,
                "groups": ["rcusers"],
            }
        if argv[0] == "usermod":
            self.users[argv[-1]]["groups"].append("rcusers")
        if argv[0] == "userdel":
            self.users.pop(argv[-1])
        return 0

    def as_user(self, info, argv, check=True):
        self.as_user_commands.append((info.name, list(argv)))
        if argv[-1] == "--version":
            version = self.claude_versions.get(info.name)
            return (0, f"{version} (Claude Code)") if version else (127, "")
        return (0, "")

    def processes_of(self, info):
        return []

    def chown(self, path, uid, gid):
        pass

    def install_claude(self, info, version):
        self.as_user_commands.append((info.name, ["install-claude", version]))
        self.claude_versions[info.name] = version


@pytest.fixture
def cfg(tmp_path):
    return ra.Config(
        etc=tmp_path / "etc",
        user_group="rcusers",
        app_group="rcman",
        claude_version="9.9.9",
        installer=None,
    )


def existing(tmp_path, name, uid=2001, groups=("rcusers",)):
    home = tmp_path / "home" / name
    home.mkdir(parents=True, exist_ok=True)
    return {name: {"uid": uid, "gid": uid, "home": home, "groups": list(groups)}}


def mapped(cfg, *users):
    uf.save(cfg.users_file, list(users))


def refused(fn, code):
    with pytest.raises(ra.AdminError) as err:
        fn()
    assert err.value.code == code


# --- add ------------------------------------------------------------------------------


def test_add_creates_installs_seeds_and_maps(cfg, tmp_path):
    system = FakeSystem(tmp_path)
    out = ra.add(cfg, system, "alice", "Alice@Example.com", admin=True)

    assert out["created"] is True
    assert [
        "useradd",
        "--create-home",
        "--user-group",
        "--groups",
        "rcusers",
        "--shell",
        "/bin/bash",
        "alice",
    ] in system.commands
    assert ("alice", ["install-claude", "9.9.9"]) in system.as_user_commands
    home = system.users["alice"]["home"]
    assert oct(home.stat().st_mode & 0o777) == "0o750"
    assert (home / "projects").is_dir()
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert uf.load(cfg.users_file) == [uf.User("alice@example.com", "alice", True)]
    assert cfg.allowlist.read_text() == "alice@example.com\n"


def test_add_again_repairs_without_recreating(cfg, tmp_path):
    system = FakeSystem(tmp_path)
    ra.add(cfg, system, "alice", "alice@example.com", admin=False)
    system.commands.clear()
    system.as_user_commands.clear()

    out = ra.add(cfg, system, "alice", "alice@example.com", admin=False)

    assert out["created"] is False
    assert not [c for c in system.commands if c[0] == "useradd"]
    assert not [
        c for _, c in system.as_user_commands if c[0] == "install-claude"
    ]  # version matches


def test_add_reinstalls_claude_when_the_version_differs(cfg, tmp_path):
    system = FakeSystem(tmp_path)
    ra.add(cfg, system, "alice", "alice@example.com", admin=False)
    system.claude_versions["alice"] = "1.0.0"
    ra.add(cfg, system, "alice", "alice@example.com", admin=False)
    assert system.claude_versions["alice"] == "9.9.9"


def test_add_keeps_existing_settings(cfg, tmp_path):
    system = FakeSystem(tmp_path, existing(tmp_path, "alice"))
    settings = system.users["alice"]["home"] / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "opus", "env": {"FOO": "1"}}))
    mapped(cfg, uf.User("alice@example.com", "alice", False))

    ra.add(cfg, system, "alice", "alice@example.com", admin=False)

    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    assert data["env"] == {"FOO": "1", "DISABLE_AUTOUPDATER": "1"}


def test_add_refuses_an_unmanaged_existing_account_unless_adopted(cfg, tmp_path):
    system = FakeSystem(tmp_path, existing(tmp_path, "carol", groups=("carol",)))
    refused(lambda: ra.add(cfg, system, "carol", "carol@example.com", admin=False), "user_exists")
    assert system.commands == []

    ra.add(cfg, system, "carol", "carol@example.com", admin=True, adopt=True)
    assert ["usermod", "--append", "--groups", "rcusers", "carol"] in system.commands


@pytest.mark.parametrize(
    "linux,email,code",
    [
        ("Bad!", "a@example.com", "linux_invalid"),
        ("alice", "nope", "email_invalid"),
    ],
)
def test_add_refuses_invalid_input_before_touching_anything(cfg, tmp_path, linux, email, code):
    system = FakeSystem(tmp_path)
    refused(lambda: ra.add(cfg, system, linux, email, admin=False), code)
    assert system.commands == [] and system.as_user_commands == []


def test_add_refuses_a_system_account(cfg, tmp_path):
    system = FakeSystem(tmp_path, existing(tmp_path, "daemonx", uid=5))
    refused(
        lambda: ra.add(cfg, system, "daemonx", "d@example.com", admin=False, adopt=True),
        "system_user",
    )
    assert system.commands == []


def test_add_refuses_conflicting_mappings(cfg, tmp_path):
    system = FakeSystem(tmp_path)
    mapped(cfg, uf.User("alice@example.com", "alice", False))
    refused(lambda: ra.add(cfg, system, "bob", "alice@example.com", admin=False), "email_taken")
    refused(lambda: ra.add(cfg, system, "alice", "other@example.com", admin=False), "linux_taken")
    assert system.commands == []


# --- delete ---------------------------------------------------------------------------


@pytest.fixture
def two_users(cfg, tmp_path):
    system = FakeSystem(
        tmp_path, {**existing(tmp_path, "alice", 2001), **existing(tmp_path, "bob", 2002)}
    )
    mapped(
        cfg, uf.User("alice@example.com", "alice", True), uf.User("bob@example.com", "bob", False)
    )
    return system


def test_delete_stops_logs_out_kills_removes_and_unmaps(cfg, two_users):
    out = ra.delete(cfg, two_users, "bob", delete_home=False, requester="alice")

    assert out == {"deleted": "bob", "home_deleted": False}
    as_bob = [c for name, c in two_users.as_user_commands if name == "bob"]
    assert ["tmux", "-L", "rcman", "kill-server"] in as_bob
    assert as_bob[-1][-2:] == ["auth", "logout"]
    order = [c[0] for c in two_users.commands]
    assert order.index("pkill") < order.index("userdel")
    assert ["userdel", "bob"] in two_users.commands
    assert [u.linux for u in uf.load(cfg.users_file)] == ["alice"]
    assert cfg.allowlist.read_text() == "alice@example.com\n"


def test_delete_with_home(cfg, two_users):
    ra.delete(cfg, two_users, "bob", delete_home=True, requester="alice")
    assert ["userdel", "--remove", "bob"] in two_users.commands


def test_delete_refuses_yourself(cfg, two_users):
    refused(
        lambda: ra.delete(cfg, two_users, "alice", delete_home=False, requester="alice"),
        "cannot_delete_self",
    )
    assert two_users.commands == []


def test_delete_refuses_the_last_admin(cfg, two_users):
    refused(
        lambda: ra.delete(cfg, two_users, "alice", delete_home=False, requester="bob"), "last_admin"
    )
    assert two_users.commands == []


def test_delete_refuses_unmanaged_users(cfg, tmp_path):
    system = FakeSystem(
        tmp_path, {**existing(tmp_path, "carol", groups=("carol",)), **existing(tmp_path, "dave")}
    )
    mapped(cfg, uf.User("carol@example.com", "carol", False))
    refused(
        lambda: ra.delete(cfg, system, "carol", delete_home=False, requester=None), "not_managed"
    )  # not in group
    refused(
        lambda: ra.delete(cfg, system, "dave", delete_home=False, requester=None), "not_managed"
    )  # not mapped
    assert system.commands == []


def test_delete_refuses_system_accounts(cfg, tmp_path):
    system = FakeSystem(tmp_path, existing(tmp_path, "root2", uid=0))
    mapped(cfg, uf.User("r@example.com", "root2", False))
    refused(
        lambda: ra.delete(cfg, system, "root2", delete_home=False, requester=None), "system_user"
    )
    assert system.commands == []


def test_delete_of_a_mapping_whose_account_is_gone_only_unmaps(cfg, tmp_path):
    system = FakeSystem(tmp_path)
    mapped(cfg, uf.User("gone@example.com", "gone", False), uf.User("a@example.com", "a", True))
    out = ra.delete(cfg, system, "gone", delete_home=False, requester=None)
    assert out == {"deleted": "gone", "home_deleted": False, "account_missing": True}
    assert system.commands == []
    assert [u.linux for u in uf.load(cfg.users_file)] == ["a"]


def test_delete_requires_a_home_decision(cfg, two_users):
    refused(
        lambda: ra.delete(cfg, two_users, "bob", delete_home=None, requester="alice"),
        "home_decision_required",
    )
    assert two_users.commands == []


# --- list -----------------------------------------------------------------------------


def test_list_reports_each_mapping_and_its_account(cfg, two_users):
    mapped(
        cfg,
        uf.User("alice@example.com", "alice", True),
        uf.User("bob@example.com", "bob", False),
        uf.User("gone@example.com", "gone", False),
    )
    users = {u["linux"]: u for u in ra.list_users(cfg, two_users)["users"]}
    assert users["alice"]["admin"] is True and users["alice"]["account"] is True
    assert users["gone"]["account"] is False


# --- configuration and command lines -------------------------------------------------


def test_production_config():
    cfg = ra.config_from_env({})
    assert cfg.etc == Path("/etc/rcman")
    assert cfg.users_file == Path("/etc/rcman/users.toml")
    assert cfg.allowlist == Path("/etc/rcman/allowed-emails")
    assert cfg.installer is None  # the official installer
    assert cfg.claude_version == (REPO / "helpers" / "claude-version").read_text().strip()


def test_approved_claude_version_is_the_recorded_one():
    from devtools import record_claude as rec

    assert ra.approved_claude_version() == rec.load_manifest()["claude_version"]


def test_user_commands_run_with_a_clean_environment(tmp_path):
    info = ra.UserInfo(name="alice", uid=2001, gid=2001, home=tmp_path, groups=["rcusers"])
    argv = ra.user_argv(info, ["claude", "--version"])
    assert argv[:4] == ["runuser", "-u", "alice", "--"]
    assert argv[4:6] == ["/usr/bin/env", "-i"]
    assert f"HOME={tmp_path}" in argv
    assert argv[-2:] == ["claude", "--version"]


def test_every_error_code_has_a_message():
    import re

    source = (REPO / "helpers" / "rc_admin.py").read_text()
    used = set(re.findall(r'AdminError\(\s*"([a-z_]+)"', source))
    assert used  # non-vacuity
    assert used <= set(ra.MESSAGES)
    assert set(ra.MESSAGES) <= used | {"usage", "internal_error", "not_root"}


def test_json_cli_requires_an_explicit_home_decision(cfg, tmp_path, capsys):
    code = ra.main_json(["del", "bob"], cfg=cfg, system=FakeSystem(tmp_path))
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "home_decision_required"


def test_json_cli_usage_errors_are_json(cfg, tmp_path, capsys):
    assert ra.main_json(["frobnicate"], cfg=cfg, system=FakeSystem(tmp_path)) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_documented_verbs_match_both_command_lines():
    import re

    doc = (REPO / "project-knowledge" / "rc-admin.md").read_text(encoding="utf-8")
    assert set(re.findall(r"`rc-admin ([a-z]+)", doc)) == set(ra.VERBS)
    guide = (REPO / "docs" / "users.md").read_text(encoding="utf-8")
    assert set(re.findall(r"rcman-user ([a-z]+)", guide)) == set(ra.VERBS)


def test_sudoers_template_is_valid_and_narrow(tmp_path):
    import subprocess

    template = (REPO / "helpers" / "sudoers.rc-admin").read_text(encoding="utf-8")
    rendered = template.replace("@ADMIN@", "/opt/rcman/helpers/rc-admin").replace(
        "@APP_USER@", "rcman"
    )
    path = tmp_path / "sudoers"
    path.write_text(rendered, encoding="utf-8")
    check = subprocess.run(
        ["/usr/sbin/visudo", "-c", "-f", str(path)], capture_output=True, text=True
    )
    assert check.returncode == 0, check.stdout + check.stderr
    rules = [line for line in rendered.splitlines() if line.strip() and not line.startswith("#")]
    assert "rcman ALL=(root) NOPASSWD: /opt/rcman/helpers/rc-admin" in rules
    assert not any("env_keep" in line or "SETENV" in line for line in rules)
