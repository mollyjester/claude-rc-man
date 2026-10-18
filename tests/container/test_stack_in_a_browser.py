"""The browser tests of frontend/e2e-stack against the whole of rcman in one container of the
tests' engine (tests/container/stack.py), the one test ``make check-ui`` runs: in Playwright's own
image, in a container that joins a stack of their own.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from devtools import held_folders as hf
from devtools.fake_claude import ACCEPTED_CODE
from tests.container import github_stand_in as stand_in
from tests.container import stack as st
from tests.container.conftest import a_stack

REPO = Path(__file__).resolve().parent.parent.parent
# Where the browser's container has the frontend's files, and the relay's folder.
FRONTEND, RELAY = "/work/frontend", "/relay"
# What all the stack's specs may take together: every test at its limit, and the two that
# make a person's first workspace at theirs (frontend/e2e-stack/timing.ts), with room over.
STACK_SPECS_WITHIN = 2400


def ran_in(stack):
    """``run`` for the relay: the command in the stack's container, its code and output."""

    def run(argv: list[str]) -> tuple[int, str]:
        done = stack.exec(*argv)
        return done.returncode, (done.stdout + done.stderr).decode(errors="replace")

    return run


@pytest.mark.stack
def test_the_whole_stack_in_a_browser(podman_service, system_image, default_image, builder_image,
                                      engine_tools, stack_prepared, toolchain, source,
                                      labelled):  # fmt: skip
    """The browser specs of frontend/e2e-stack, in Playwright's own image, in a container that
    joins a stack of their own, signing in to the fake claude with the code it accepts; then
    ``stack.py check``. Run on demand by ``make check-ui``, the ``stack`` marker keeping it out
    of ``make test-container``."""
    with (
        a_stack(
            podman_service, f"rcman-test-stack-ui-{os.getpid()}", system_image, default_image,
            builder_image, engine_tools, stack_prepared, toolchain, source, labelled,
        ) as stack,
        hf.held("rcrelay-") as folder,
    ):  # fmt: skip
        with st.command_relay(ran_in(stack), folder) as path:
            command = ["node", "/work/tests/container/stack_exec.mjs", f"{RELAY}/{path.name}"]
            environment = {
                "RCMAN_STACK_PORT": "443",
                "RCMAN_FAKE_CLAUDE_CODE": ACCEPTED_CODE,
                "RCMAN_STACK_GITHUB": json.dumps({
                    "token": stand_in.STACK_TOKEN,
                    "public": f"https://github.com/{stand_in.STACK_OWNER}/{stand_in.STACK_PUBLIC}",
                    "private": f"https://github.com/{stand_in.STACK_OWNER}/{stand_in.STACK_PRIVATE}",
                }),
                "RCMAN_STACK_EXEC": json.dumps(command),
                "CI": "1",
            }  # fmt: skip
            browser = podman_service.cli(
                "run", "--rm", f"--network={stack.network}", "--ipc=private", *labelled,
                *(arg for key, value in environment.items() for arg in ("-e", f"{key}={value}")),
                "-v", f"{REPO / 'frontend'}:{FRONTEND}", "-v", f"{source / 'tests'}:/work/tests:ro",
                "-v", f"{folder}:{RELAY}", "-w", FRONTEND, st.PLAYWRIGHT_IMAGE,
                "npx", "playwright", "test", "--config", "playwright.stack.config.ts",
                "--reporter=line", timeout=STACK_SPECS_WITHIN,
            )  # fmt: skip
        said = (browser.stdout + browser.stderr).decode(errors="replace")
        assert browser.returncode == 0, said[-12000:]
        assert " passed" in said and " skipped" not in said, said[-4000:]
        totals = re.findall(
            r"(?m)^\s*(\d+ (?:passed|failed|flaky|skipped)\b.*)$", browser.stdout.decode()
        )
        print("Playwright:", "; ".join(totals))  # its own totals, for the run's report
        stack.verb("check")
