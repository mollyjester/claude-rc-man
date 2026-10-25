"""rcman installed by its real installer, as root, in a stubbed installation: a container of the
system image (tests/container/system/) on the tests' engine (tests/podman_service.py), with no
network, real accounts, files, modes and ``runuser``, and ``systemctl``, ``loginctl`` and rcman's
``podman`` the recorded stand-ins of tests/container/stand_ins.py, under which the installer's
units really run as their accounts.

The checkout's files are at /src (tests/snapshot.py); a user ``dev`` clones them and runs
``install.sh`` through the real path, with the fake claude as the Claude Code release it downloads
(tests/container/installed.py). The toolchain comes from the user's cache, filled with the
installer's own download code (the ``toolchain`` fixture); the frontend's and the backend's
packages from caches filled beforehand in a container of the same image with the network
(``offline_caches``); tmux is the one built on the engine with the installer's own argument list,
which the stand-in podman hands the installer. The first installation is made once; each test then
runs its check of tests/container/install_checks.py, of the same name, as root in the container,
in order, on that installation. What needs systemd, rcman's own rootless Podman, the kernel's
limits or the journal is checked by hand (project-knowledge/real-claude-checklist.md); what needs
people and workspaces runs in the whole stack (tests/container/test_stack_in_podman.py).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from devtools import held_folders as hf
from installer import install as ins

# The installer's toolchain, where the container has it.
TOOLCHAIN = Path("/opt/rcman/toolchain")
# Seconds a check may take: some run the installer again, which may build a release.
CHECKED_WITHIN = 2400
# The programs the stand-ins are mounted over.
STOOD_IN = ("/usr/bin/systemctl", "/usr/bin/loginctl", "/usr/bin/podman")
STAND_INS = "/var/lib/rcman-test-stand-ins"


def ran(done, what: str) -> str:
    output = (done.stdout + done.stderr).decode(errors="replace")
    assert done.returncode == 0, f"{what}:\n{output[-12000:]}"
    return output


@pytest.fixture(scope="session")
def offline_caches(podman_service, system_image, source, toolchain, labelled):
    """npm's cache of the frontend's packages and uv's of the backend's, filled in a container of
    the system image with the engine's own network, as the installer would fetch them: so that
    the installation, which has no network, installs them from the caches alone."""
    with hf.held("rccache-") as folder:
        os.chmod(folder, 0o755)
        node, uv = ins.node_bin(TOOLCHAIN), ins.uv_binary(TOOLCHAIN)
        script = (
            "set -e; cp -a /src /tmp/s; cd /tmp/s/frontend; "
            f"PATH={node}:$PATH npm ci --no-audit --no-fund --cache /cache/npm; cd /tmp/s; "
            f"UV_CACHE_DIR=/cache/uv {uv} sync --frozen --no-dev --no-editable "
            "--python /usr/bin/python3; chmod -R a+rwX /cache"
        )
        done = podman_service.cli(
            "run", "--rm", "--pull=never", "--network=host", *labelled, "-v", f"{source}:/src:ro",
            "-v", f"{toolchain}:{TOOLCHAIN}:ro", "-v", f"{folder}:/cache", system_image,
            "sh", "-c", script, timeout=2400,
        )  # fmt: skip
        ran(done, "the caches were not filled")
        yield folder


class Installation:
    """The stubbed installation's container on the engine."""

    def __init__(self, engine, name: str):
        self.engine, self.name = engine, name

    def exec(self, *argv: str, stdin: bytes | None = None, timeout: float = CHECKED_WITHIN):
        return self.engine.cli(
            "exec", "-i", "-w", "/src", "-e", "PYTHONDONTWRITEBYTECODE=1", self.name, *argv,
            input=stdin, timeout=timeout,
        )  # fmt: skip

    def check(self, name: str) -> None:
        """The check of tests/container/install_checks.py called ``name``, as root."""
        done = self.exec("python3", "tests/container/install_checks.py", name)
        ran(done, f"the check {name} failed")


@pytest.fixture(scope="module")
def installation(
    podman_service, system_image, labelled, source, toolchain, offline_caches, engine_tools
):
    stand_in = source / "tests" / "container" / "stand_ins.py"
    name = f"rcman-test-install-{os.getpid()}"
    mounts = [arg for program in STOOD_IN for arg in ("-v", f"{stand_in}:{program}:ro")]
    # A copy of the user's toolchain: the installer, as root there, takes its lock in it and
    # fills what is missing, which the user's own cache never gets.
    # In a folder of the held one's: the held folder's own lock is a file of the name the
    # installer's lock has.
    with hf.held("rctoolchain-") as held_copy:
        toolchain_copy = held_copy / "toolchain"
        shutil.copytree(toolchain, toolchain_copy, symlinks=True)
        os.chmod(held_copy, 0o755)
        made = podman_service.cli(
            "run", "-d", "--init", "--pull=never", "--network=none", *labelled, "--name", name,
            "-v", f"{source}:/src:ro", "-v", f"{toolchain_copy}:{TOOLCHAIN}",
            "-v", f"{offline_caches}:/cache", *mounts, system_image, "sleep", "infinity",
            timeout=300,
        )  # fmt: skip
        try:
            ran(made, "the installation's container did not start")
            found = Installation(podman_service, name)
            tmux = (engine_tools / "bin" / "tmux").read_bytes()
            put = found.exec(
                "sh", "-c", f"mkdir -p -m 777 {STAND_INS} && cat > {STAND_INS}/tmux", stdin=tmux
            )
            ran(put, "tmux was not handed to the stand-in")
            found.check("first")
            yield found
        finally:
            podman_service.cli("rm", "--force", "--time=0", name, timeout=300)


def test_the_first_installation_puts_rcman_in_caddy_and_restarts_it(installation):
    installation.check("test_the_first_installation_puts_rcman_in_caddy_and_restarts_it")


def test_installing_leaves_nothing_owned_by_root_in_the_clone(installation):
    installation.check("test_installing_leaves_nothing_owned_by_root_in_the_clone")


def test_installs_the_clones_commit_as_the_current_release(installation):
    installation.check("test_installs_the_clones_commit_as_the_current_release")


def test_the_release_holds_only_runtime_files_owned_by_root(installation):
    installation.check("test_the_release_holds_only_runtime_files_owned_by_root")


def test_accounts_exist(installation):
    installation.check("test_accounts_exist")


def test_rcmans_subordinate_ids_and_linger_are_written(installation):
    installation.check("test_rcmans_subordinate_ids_and_linger_are_written")


def test_the_limits_drop_ins_are_written_with_the_installers_text(installation):
    installation.check("test_the_limits_drop_ins_are_written_with_the_installers_text")


def test_the_unit_runs_rcman_with_no_new_privileges_and_its_podman_socket(installation):
    installation.check("test_the_unit_runs_rcman_with_no_new_privileges_and_its_podman_socket")


def test_the_tools_are_static_and_root_s(installation):
    installation.check("test_the_tools_are_static_and_root_s")


def test_the_instructions_for_claude_are_roots_and_readable_by_everyone(installation):
    installation.check("test_the_instructions_for_claude_are_roots_and_readable_by_everyone")


def test_the_tools_are_outside_rcmans_home_and_root_leaves_nothing_there_of_its_own(installation):
    installation.check(
        "test_the_tools_are_outside_rcmans_home_and_root_leaves_nothing_there_of_its_own"
    )


def test_the_default_and_builder_images_are_built_and_named_in_rcmans_settings(installation):
    installation.check("test_the_default_and_builder_images_are_built_and_named_in_rcmans_settings")


def test_rcmans_subordinate_range_covers_the_builders_slot(installation):
    installation.check("test_rcmans_subordinate_range_covers_the_builders_slot")


def test_the_claude_store_holds_the_tested_version(installation):
    installation.check("test_the_claude_store_holds_the_tested_version")


def test_the_claude_store_is_rcmans_and_readable_by_every_workspace(installation):
    installation.check("test_the_claude_store_is_rcmans_and_readable_by_every_workspace")


def test_installing_again_leaves_current_on_the_version_rcman_approved(installation):
    installation.check("test_installing_again_leaves_current_on_the_version_rcman_approved")


def test_settings_are_saved_privately(installation):
    installation.check("test_settings_are_saved_privately")


def test_configuration_files_and_their_owners(installation):
    installation.check("test_configuration_files_and_their_owners")


def test_the_first_administrator_is_there(installation):
    installation.check("test_the_first_administrator_is_there")


def test_an_administrator_runs_rcman_admin_by_name_as_root(installation):
    installation.check("test_an_administrator_runs_rcman_admin_by_name_as_root")


def test_the_socket_directory_admits_only_its_group(installation):
    installation.check("test_the_socket_directory_admits_only_its_group")


def test_the_socket_group_is_kept_to_caddy_alone(installation):
    installation.check("test_the_socket_group_is_kept_to_caddy_alone")


def test_no_listener_lets_a_local_user_impersonate(installation):
    installation.check("test_no_listener_lets_a_local_user_impersonate")


def test_with_web_upgrade_off_no_upgrade_unit_exists(installation):
    installation.check("test_with_web_upgrade_off_no_upgrade_unit_exists")


def test_web_upgrade_turned_on_enables_its_path_unit_and_installs_the_wrapper(installation):
    installation.check("test_web_upgrade_turned_on_enables_its_path_unit_and_installs_the_wrapper")


def test_a_wrapper_run_with_no_sudo_user_writes_the_status(installation):
    installation.check("test_a_wrapper_run_with_no_sudo_user_writes_the_status")


def test_a_dirty_clone_gives_a_failed_status(installation):
    installation.check("test_a_dirty_clone_gives_a_failed_status")


def test_running_again_changes_nothing(installation):
    installation.check("test_running_again_changes_nothing")


def test_saved_settings_need_no_answers(installation):
    installation.check("test_saved_settings_need_no_answers")


def test_a_dirty_clone_is_refused(installation):
    installation.check("test_a_dirty_clone_is_refused")


def test_an_update_of_install_sh_restarts_it_and_builds_a_new_release(installation):
    installation.check("test_an_update_of_install_sh_restarts_it_and_builds_a_new_release")


def test_rollback_returns_to_the_previous_release(installation):
    installation.check("test_rollback_returns_to_the_previous_release")


def test_a_rollback_to_a_release_older_than_the_database_is_refused(installation):
    installation.check("test_a_rollback_to_a_release_older_than_the_database_is_refused")


def test_a_clone_that_cannot_fast_forward_is_refused(installation):
    installation.check("test_a_clone_that_cannot_fast_forward_is_refused")
