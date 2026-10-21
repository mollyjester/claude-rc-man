"""backend/rcman/podman/publish.sh: what the tools' podman and docker say of a port published in
a workspace, where a container shares the workspace's network and publishing gives it nothing.
It runs here as the wrapper runs it, sourced after the wrapper's own definitions by the static
busybox's shell, with no PATH: it uses the shell's built-ins alone. Its lists of Podman's options
are held to Podman's help here by a parser, and to the help of the default image's Podman by
tests/container/test_nest_in_podman.py."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

from installer import install as ins

REPO = Path(__file__).resolve().parent.parent
PODMAN_SOURCES = REPO / "backend" / "rcman" / "podman"
WRAPPER = PODMAN_SOURCES / "podman"
# The wrapper's lines where it runs Podman's own, which there is: the image has none before it.
NO_PODMAN = 'if [ ! -x "$PODMAN" ]; then\n'
SOURCED = f'. "$HERE/{ins.PODMAN_PUBLISH}"\n'
ROOTLESS = 'if [ "$($B id -u)" != 0 ]; then\n'
# The subcommands publish.sh reads, as Podman's help is asked for them.
SUBCOMMANDS = (("run",), ("create",), ("container", "run"), ("container", "create"),
               ("pod", "create"))  # fmt: skip
# Podman's alias of --network, which its help does not show.
HIDDEN = {"--net"}
# Podman's help for each of SUBCOMMANDS and for podman itself, its global options, as the
# default image's Podman gives it (held to it by tests/container/test_nest_in_podman.py).
RECORDED = REPO / "tests" / "fakes" / "podman-help"
GLOBAL_HELP = "podman.txt"


def help_file(subcommand: tuple[str, ...]) -> str:
    return "-".join(subcommand) + ".txt"


def explained(name: str, *args: str, then: str = "echo ran") -> subprocess.CompletedProcess:
    """publish.sh run as the wrapper ``name`` (podman or docker) runs it for ``args``: after the
    wrapper's definitions, with HERE the sources, and then ``then``, as what comes after it."""
    text = WRAPPER.read_text(encoding="utf-8")
    head = text[: text.index(NO_PODMAN)]
    script = f"{head}HERE={shlex.quote(str(PODMAN_SOURCES))}\n{SOURCED}{then}\n"
    return subprocess.run(
        [str(ins.BUSYBOX), "sh", "-c", script, f"/opt/rcman/podman/bin/{name}", *args],
        capture_output=True, text=True, env={"PATH": "/nowhere"}, timeout=30,
    )  # fmt: skip


def said(name: str, flag: str, shares: str = "a container") -> str:
    return (
        f"{name}: {flag} publishes no port here: {shares} shares this workspace's network, so "
        "what it listens on is on the workspace's own ports; reach it at localhost:<the port it "
        "listens on>\n"
    )


@pytest.mark.parametrize(
    ("args", "flag"),
    [
        (["run", "-p", "80:80", "img"], "-p"),
        (["run", "--publish", "80:80", "img"], "--publish"),
        (["run", "--publish=80:80", "img"], "--publish"),
        (["run", "-p80:80", "img"], "-p"),
        (["run", "-p=80:80", "img"], "-p"),
        (["run", "-P", "img"], "-P"),
        (["run", "--publish-all", "img"], "--publish-all"),
        (["run", "--publish-all=true", "img"], "--publish-all"),
        (["run", "-dp", "443:443", "img"], "-p"),
        (["run", "-itdp", "443:443", "img"], "-p"),
        (["run", "-dP", "img"], "-P"),
        (["run", "--rm", "--name", "web", "-e", "A=1", "-p", "1:1", "img", "sh"], "-p"),
        (["--log-level", "debug", "--root=/x", "--remote", "run", "-p", "1:1", "img"], "-p"),
        (["container", "run", "-p", "1:1", "img"], "-p"),
        (["create", "-p", "1:1", "img"], "-p"),
        (["container", "create", "--publish=1:1", "img"], "--publish"),
        (["run", "--network=host", "-p", "1:1", "img"], "-p"),
        (["run", "--network", "host", "-p", "1:1", "img"], "-p"),
        (["run", "--net=host", "-p", "1:1", "img"], "-p"),
        # global options after the subcommand, which Podman takes there too
        (["run", "--log-level", "debug", "-p", "1:1", "img"], "-p"),
        (["run", "--root=/x", "-p", "1:1", "img"], "-p"),
        (["run", "--root", "/x", "--runroot", "/y", "-p", "1:1", "img"], "-p"),
        (["run", "-r", "-p", "1:1", "img"], "-p"),
        (["run", "-rdp", "1:1", "img"], "-p"),
        # a letter taking no value takes what follows an =
        (["run", "-dP=true", "img"], "-P"),
        (["run", "-P=1", "img"], "-P"),
    ],
)
@pytest.mark.parametrize("name", ["podman", "docker"])
def test_a_published_port_is_named(name, args, flag):
    ran = explained(name, *args)
    assert (ran.returncode, ran.stdout) == (0, "ran\n"), ran  # and Podman runs as before
    assert ran.stderr == said(name, flag), ran


def test_a_port_a_pod_publishes_is_named_for_the_pod():
    ran = explained("podman", "pod", "create", "-p", "1:1", "web")
    assert (ran.returncode, ran.stdout) == (0, "ran\n"), ran
    assert ran.stderr == said("podman", "-p", "a pod"), ran


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--publish-all=false", "img"],
        ["run", "--network", "bridge", "-p", "1:1", "img"],
        ["run", "--net=x", "-p", "1:1", "img"],
        ["run", "--pod", "p", "-p", "1:1", "img"],
        ["run", "--pod", "new:x", "-p", "1:1", "img"],
        ["run", "--pod-id-file", "/x/id", "-p", "1:1", "img"],
        ["run", "-P=false", "img"],
        ["run", "-dP=false", "img"],
        ["run", "-P", "--publish-all=false", "img"],
        ["run", "--log-level", "-p", "img"],  # -p is the value of --log-level
        ["run", "img", "echo", "-p", "1:1"],
        ["run", "--env", "X=-p", "img"],
        ["run", "--name", "-p", "img"],
        ["run", "-e", "-p", "img"],
        ["run", "-dep", "img"],
        ["run", "-xp", "1:1", "img"],  # a letter that is no option: Podman says so
        ["run", "--", "-p", "1:1"],
        ["run", "--rm", "img", "-P"],
        ["compose", "up", "-p", "x"],
        ["ps", "-p"],
        ["--root", "run", "ps", "-p", "1:1"],  # run is the value of --root
        ["pod", "ps", "-p"],
        ["container", "ls", "-p"],
        [],
    ],
)
def test_nothing_is_named(args):
    ran = explained("podman", *args)
    assert (ran.returncode, ran.stdout, ran.stderr) == (0, "ran\n", ""), ran


def test_the_wrapper_sources_publish_sh_after_the_no_podman_branch():
    """Only where the image's Podman runs, and before root and the others go their ways."""
    text = WRAPPER.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    assert lines.count(SOURCED) == 1, "publish.sh is sourced once, at the top level"
    assert text.index(NO_PODMAN) < text.index(SOURCED) < text.index(ROOTLESS)
    assert "fi\n" in text[text.index(NO_PODMAN) : text.index(SOURCED)]  # the branch is closed


# --- the lists of Podman's options -------------------------------------------------------------

# An option of Podman's help: its letter, if it has one, its name, and the kind of value it takes,
# if it takes one.
HELP_OPTION = re.compile(r"^\s+(?:-(\w), )?--([\w-]+)(?: (\S+))?(?:\s{2,}|$)")


def help_options(text: str) -> tuple[set[str], set[str], set[str]]:
    """The options a Podman help ``text`` lists: those taking a value, by name; the letters of
    those taking a value; and the letters of those taking none. One whose value may be left out
    (``string[="v"]``, a value of its own given after an = alone) takes none as the next
    argument, and is one of those taking none."""
    valued, valued_letters, letters = set(), set(), set()
    for line in text.splitlines():
        found = HELP_OPTION.match(line)
        if not found:
            continue
        letter, name, kind = found.groups()
        kind = None if kind and "[=" in kind else kind
        if kind:
            valued.add(f"--{name}")
        if letter:
            (valued_letters if kind else letters).add(letter)
    return valued, valued_letters, letters


def wrapper_options() -> dict[str, set[str]]:
    """publish.sh's own lists, and the wrapper's global options taking a value (VALUED), as
    the wrapper's shell reads them."""
    names = ("PUBLISH_VALUED", "PUBLISH_VALUED_LETTERS", "PUBLISH_LETTERS",
             "PUBLISH_GLOBAL_LETTERS", "VALUED")  # fmt: skip
    shown = explained("podman", then=" ".join(f'echo "${name}";' for name in names))
    assert shown.returncode == 0 and shown.stderr == "", shown
    values = shown.stdout.split("\n")
    return {
        "valued": set(values[0].split()),
        "valued_letters": set(values[1]),
        "letters": set(values[2]),
        "global_letters": set(values[3]),
        "global_valued": set(values[4].split()),
    }


def option_problems(helps: list[str], global_help: str) -> list[str]:
    """How publish.sh's lists are not the options of the help texts ``helps`` together, but for
    HIDDEN, which a help does not show and the wrapper knows; and how they and the wrapper's
    VALUED are not the global options of ``global_help`` that Podman takes after them."""
    asked = [help_options(text) for text in helps]
    valued = set().union(*(a[0] for a in asked))
    valued_letters = set().union(*(a[1] for a in asked))
    letters = set().union(*(a[2] for a in asked))
    own = wrapper_options()
    problems = [f"{name} is in the help, and no longer hidden" for name in sorted(valued & HIDDEN)]
    problems += [
        f"{name} takes a value, and is not listed" for name in sorted(valued - own["valued"])
    ]
    problems += [
        f"{name} is listed, and takes no value in the help"
        for name in sorted(own["valued"] - valued - HIDDEN)
    ]
    if (own["valued_letters"], own["letters"]) != (valued_letters, letters):
        problems.append(
            f"letters: listed {sorted(own['valued_letters'])} {sorted(own['letters'])}, "
            f"in the help {sorted(valued_letters)} {sorted(letters)}"
        )
    global_valued, global_valued_letters, global_letters = help_options(global_help)
    problems += [
        f"{name}, a global option, takes a value, and the wrapper's VALUED does not list it"
        for name in sorted(global_valued - own["global_valued"])
    ]
    # the global letters a subcommand does not take for one of its own
    unshadowed = (global_valued_letters | global_letters) - valued_letters - letters
    problems += [
        f"-{letter}, a global option, takes a value, which no list says"
        for letter in sorted(unshadowed & global_valued_letters)
    ]
    if own["global_letters"] != unshadowed & global_letters:
        problems.append(
            f"global letters: listed {sorted(own['global_letters'])}, "
            f"in the help {sorted(unshadowed & global_letters)}"
        )
    return problems


def recorded() -> tuple[list[str], str]:
    """The recorded helps of SUBCOMMANDS, and of podman itself."""
    helps = [(RECORDED / help_file(sub)).read_text(encoding="utf-8") for sub in SUBCOMMANDS]
    return helps, (RECORDED / GLOBAL_HELP).read_text(encoding="utf-8")


HELP = """\
Options:
      --add-host strings                         Add a custom host-to-IP mapping (host:ip) (default [])
  -a, --attach strings                           Attach to STDIN, STDOUT or STDERR
  -d, --detach                                   Run container in background and print container ID
      --ip6 string                               Specify a static IPv6 address for the container
  -m, --memory <number>[<unit>]                  Memory limit (format: <number>[<unit>], where unit = b)
      --rm                                       Remove container and any anonymous volumes
      --tls-verify
      --gpus string[="all"]                      A value given after an = alone
  -o, --optional string[="v"]                    The same, with a letter
"""  # noqa: E501 (as Podman prints it)


def test_a_help_texts_options_are_read_with_their_letters_and_values():
    assert help_options(HELP) == (
        {"--add-host", "--attach", "--ip6", "--memory"},
        {"a", "m"},
        {"d", "o"},
    )


def test_the_wrappers_option_lists_are_those_of_podmans_recorded_help():
    helps, global_help = recorded()
    assert all("--publish strings" in text for text in helps)  # the control: each one read
    assert "-r, --remote" in global_help
    assert option_problems(helps, global_help) == []


def test_an_option_the_wrapper_does_not_list_is_found():
    """The twin: a recorded help with an option planted in it, and helps with none at all."""
    helps, global_help = recorded()
    planted = [helps[0] + "      --planted string   planted\n", *helps[1:]]
    assert option_problems(planted, global_help) == ["--planted takes a value, and is not listed"]
    planted_global = global_help + "  -z, --zed   planted\n"
    assert option_problems(helps, planted_global) == [
        "global letters: listed ['r'], in the help ['r', 'z']"
    ]
    problems = option_problems([""], "")
    assert "--publish is listed, and takes no value in the help" in problems
    assert any(p.startswith("letters:") for p in problems)
