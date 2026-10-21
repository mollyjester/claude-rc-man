"""The application as it is started in production."""

import os
import subprocess
import sys

import pytest


def test_packages_import_from_the_environment_alone():
    """Not through pytest's pythonpath: from another directory, with no PYTHONPATH."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", "import rcman.main, helpers.rules, helpers.screens"],
        cwd="/",
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_openapi_lists_the_api(client):
    paths = client.get("/api/openapi.json").json()["paths"]
    assert "/api/workspaces" in paths and "/api/people/{person}" in paths


# --- the sizes the tests inject stay out of the product ---------------------------------------


def test_the_service_runs_podman_with_the_products_range_network_and_slices():
    from rcman import main
    from rcman import runtime as rt
    from rcman.settings import Settings

    config = main.podman_runtime(Settings(public_origin="https://x.example.com")).config
    assert (config.range, config.network, config.slices) == (rt.RANGE, rt.NETWORK, True)
    defaults = rt.PodmanConfig(config.socket, config.image, config.tools_dir, config.claude_store)
    assert all(getattr(config, n) == getattr(defaults, n) for n in rt.INJECTED)


def injected_in(root):
    """Every call in the Python files under ``root`` that gives PodmanConfig, replaces in one or
    sets on one a field the tests inject (runtime.INJECTED), as (file, line): PodmanConfig and
    ``replace`` by any name a module imports them as, and ``setattr`` and ``object.__setattr__``
    of such a field by its name; and every call that gives one of the other sizes (SIZED) a value
    not on the allow-list (``_passed_on``)."""
    import ast
    from pathlib import Path

    from rcman import runtime as rt

    found = []
    for path in sorted(Path(root).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        configs, replaces = {"PodmanConfig"}, {"replace"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "PodmanConfig":
                        configs.add(alias.asname or alias.name)
                    if alias.name == "replace":
                        replaces.add(alias.asname or alias.name)
        for node, params in _calls(tree, frozenset()):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            # A spread could hold anything: in PodmanConfig it counts. A replace of anything
            # else (a Limits, say) counts only where it names an injected field.
            spread = name in configs and any(k.arg is None for k in node.keywords)
            named = name in configs | replaces and any(k.arg in rt.INJECTED for k in node.keywords)
            set_by_name = (
                name in ("setattr", "__setattr__")
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in rt.INJECTED
            )
            sized = name in SIZED and any(
                k.arg == SIZED[name] and not _passed_on(k.value, k.arg, params)
                for k in node.keywords
            )
            if spread or named or set_by_name or sized:
                found.append((str(path), node.lineno))
    return found


# The calls that take a size a test engine gives, and the keyword it is given as.
SIZED = {
    "Workspaces": "builder_slot",
    "create_app": "builder_slot",
    "HostRuntime": "range",
    "add": "last_slot",
    "plan": "size",
    "resolve_user": "size",
    "run_canary": "slot",
}
# The product's own sizes, as product code names them.
PRODUCT_SIZES = {"people.BUILDER_SLOT", "BUILDER_SLOT", "RANGE", "rt.RANGE", "runtime.RANGE",
                 "MAX_PEOPLE", "people.MAX_PEOPLE"}  # fmt: skip
# What product code may pass on: what the service was given, the runtime's range, and the
# people below a builder slot.
PASSED_ON = {"self.builder_slot", "self.workspaces.builder_slot", "self.runtime.id_range"}
BELOW_A_BUILDER_SLOT = r"[A-Za-z_][\w.]*\.builder_slot - 1"


def _calls(node, params):
    """Every call under ``node``, with the parameter names of the function it is in."""
    import ast

    for child in ast.iter_child_nodes(node):
        inner = params
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = child.args
            inner = frozenset(
                a.arg for a in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
            )
        if isinstance(child, ast.Call):
            yield child, params
        yield from _calls(child, inner)


def _passed_on(value, keyword: str, params) -> bool:
    """Whether a sized keyword's ``value`` is on the allow-list: the enclosing function's own
    parameter of that name, what the service was given, or the product's own size."""
    import ast
    import re

    if isinstance(value, ast.Name) and value.id == keyword and keyword in params:
        return True
    text = ast.unparse(value)
    return (
        text in PASSED_ON
        or text in PRODUCT_SIZES
        or re.fullmatch(BELOW_A_BUILDER_SLOT, text) is not None
    )


# The parameters that take a size a test engine gives, and the functions they are checked in
# (None: any function); their default is the product's size, or None meaning it.
DEFAULTED = {
    "builder_slot": None,
    "last_slot": None,
    "range": {"__init__"},
    "size": {"plan", "resolve_user"},
    "slot": {"run_canary"},
}


def sized_defaults(root):
    """Every function under ``root`` with a sized parameter (DEFAULTED) whose default is not the
    product's own size or None, as (file, line)."""
    import ast
    from pathlib import Path

    found = []
    for path in sorted(Path(root).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            arguments = node.args
            positional = [*arguments.posonlyargs, *arguments.args]
            pairs = list(zip(positional[len(positional) - len(arguments.defaults) :],
                             arguments.defaults, strict=True))  # fmt: skip
            pairs += [(a, d) for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults,
                                             strict=True) if d is not None]  # fmt: skip
            for argument, default in pairs:
                where = DEFAULTED.get(argument.arg, ())
                if where != () and (where is None or node.name in where):
                    none = isinstance(default, ast.Constant) and default.value is None
                    if not none and ast.unparse(default) not in PRODUCT_SIZES:
                        found.append((str(path), node.lineno))
    return found


PRODUCT_ROOTS = ("backend", "helpers", "installer")


def test_no_product_code_gives_podman_a_size_of_the_tests():
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    for root in PRODUCT_ROOTS:
        assert list((repo / root).rglob("*.py")), root  # non-vacuity: the root holds code
        assert injected_in(repo / root) == [], root
        assert sized_defaults(repo / root) == [], root


def test_the_size_guard_finds_a_planted_range_network_slices_and_a_spread(tmp_path):
    from rcman import runtime as rt

    clean = tmp_path / "clean.py"
    clean.write_text(
        "PodmanConfig(socket, image, tools, store, builder_image='b')\n"
        "dataclasses.replace(held, **changes)\n"
        "from dataclasses import replace as r\nr(held, memory=None)\n"
        "setattr(thing, 'other', 1)\n"
        "people.add(con, email, admin, last_slot=service.builder_slot - 1)\n"
        "dc.plan(config, facts, 1, work, size=self.runtime.id_range)\n"
        "def make(settings, runtime, *, builder_slot=people.BUILDER_SLOT):\n"
        "    return Workspaces(settings, runtime, builder_slot=builder_slot)\n"
        "Workspaces(settings, runtime, builder_slot=people.BUILDER_SLOT)\n"
    )
    assert sized_defaults(tmp_path) == []
    assert injected_in(tmp_path) == []
    for number, planted in enumerate(
        [*(f"PodmanConfig(s, i, t, c, {name}=x)" for name in rt.INJECTED),
         "rt.PodmanConfig(s, i, t, c, **sizes)",
         "dataclasses.replace(config, network='host')",
         "from dataclasses import replace as r\nr(config, range=8)",
         "from rcman.runtime import PodmanConfig as P\nP(s, i, t, c, slices=False)",
         "object.__setattr__(config, 'slices', False)",
         "setattr(config, 'range', 8)",
         *(f"{name}(a, b, {keyword}=6)" for name, keyword in SIZED.items()),
         "people.add(con, email, admin, last_slot=8192 // 8192 + 4)"]
    ):  # fmt: skip
        (tmp_path / f"plant{number}.py").write_text(planted + "\n")
        line = planted.count("\n") + 1  # the call is the plant's last line
        assert (str(tmp_path / f"plant{number}.py"), line) in injected_in(tmp_path), planted


# --- the workspaces' seccomp profile is checked when the service starts ------------------------


def test_a_stale_or_missing_seccomp_profile_is_said_loudly_and_a_current_one_is_not(
    tmp_path, caplog
):
    from rcman import main

    from helpers import seccomp

    recorded = __import__("pathlib").Path(__file__).parent / "fakes" / "podman-seccomp.json"
    tools = tmp_path / "tools"
    tools.mkdir()
    with caplog.at_level("ERROR", logger=main.workspaces_log.name):
        main.check_seccomp(tools, recorded)  # missing
        (tools / seccomp.NAME).write_text("{}\n")
        main.check_seccomp(tools, recorded)  # another
        (tools / seccomp.NAME).write_text(seccomp.workspace_profile(recorded.read_text()))
        main.check_seccomp(tools, recorded)  # the control: current
    said = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(said) == 2, said
    assert "is not there" in said[0] and "not the one derived" in said[1]
    assert all("run the installer again" in line for line in said)


def test_a_missing_instructions_file_is_said_in_the_journal(tmp_path, caplog):
    """No workspace can be made, nor any lost container made again, until the installer runs
    again (rcman/runtime.py INSTRUCTIONS_MISSING)."""
    from pathlib import Path

    from rcman import main
    from rcman import runtime as rt

    from tests.test_runtime import told

    tools = tmp_path / "tools"
    runtime = rt.PodmanRuntime(rt.PodmanConfig(Path("/s"), "i", tools, Path("/c")))
    with caplog.at_level("ERROR", logger=main.workspaces_log.name):
        main.check_instructions(runtime)  # missing
        told(tools)
        main.check_instructions(runtime)  # the control: there
    said = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert said == [rt.INSTRUCTIONS_MISSING]


def test_the_service_checks_the_instructions_of_its_runtime(tmp_path, monkeypatch, settings):
    from rcman import main
    from rcman import runtime as rt

    from tests.conftest import host_runtime

    checked = []
    monkeypatch.setattr(main, "check_instructions", checked.append)
    main.create_app(settings)
    assert len(checked) == 1 and isinstance(checked[0], rt.PodmanRuntime)
    assert checked[0].config.tools_dir == settings.tools_dir
    with host_runtime(tmp_path) as runtime:
        main.create_app(settings, runtime=runtime)
    assert checked[1:] == [runtime]  # whose answer is that it needs none


def test_the_service_checks_the_profile_of_its_own_podman_and_not_of_a_given_runtime(
    tmp_path, monkeypatch, settings
):
    from rcman import main

    from tests.conftest import host_runtime

    checked = []
    monkeypatch.setattr(main, "check_seccomp", lambda tools, *more: checked.append(tools))
    main.create_app(settings)
    assert checked == [settings.tools_dir]
    with host_runtime(tmp_path) as runtime:
        main.create_app(settings, runtime=runtime)
    assert checked == [settings.tools_dir]  # once: the test's runtime is not checked


def test_the_service_gives_builds_the_products_builder_slot_and_people_the_slots_below_it(settings):
    from rcman import main, people
    from rcman import runtime as rt

    app = main.create_app(settings)
    assert app.state.workspaces.builder_slot == people.BUILDER_SLOT == people.MAX_PEOPLE + 1
    assert app.state.workspaces.runtime.id_range == rt.RANGE


@pytest.mark.parametrize(
    "planted",
    [
        "SMALL = 8192\ndef f(settings, r):\n    return Workspaces(settings, r, builder_slot=SMALL)",
        "people.add(con, email, admin, last_slot=people.BUILDER_SLOT - 59)",
        "def f(settings, r, slot):\n    return Workspaces(settings, r, builder_slot=slot)",
        "Workspaces(settings, r, builder_slot=builder_slot)",  # no parameter of that name here
        "dc.plan(config, facts, 1, work, size=other.id_range)",
    ],
)
def test_the_size_guard_finds_a_value_off_the_allow_list(tmp_path, planted):
    (tmp_path / "plant.py").write_text(planted + "\n")
    assert injected_in(tmp_path), planted


@pytest.mark.parametrize(
    "planted",
    [
        "class W:\n    def __init__(self, s, *, builder_slot: int = 6):\n        pass",
        "def add(con, email, admin, *, last_slot=5):\n    pass",
        "def plan(config, facts, *, size=8192):\n    pass",
        "def run_canary(self, d, n, v, *, slot=6):\n    pass",
    ],
)
def test_the_defaults_guard_finds_a_changed_default(tmp_path, planted):
    (tmp_path / "plant.py").write_text(planted + "\n")
    assert sized_defaults(tmp_path), planted
    control = planted.replace("= 6", "= people.BUILDER_SLOT").replace("=5", "=None")
    control = control.replace("=8192", "=RANGE").replace("slot=6", "slot=None")
    (tmp_path / "plant.py").write_text(control + "\n")
    assert sized_defaults(tmp_path) == [], control
