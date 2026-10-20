"""RFC 9457 problem details for every error the API returns."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError, WrapValidator
from pydantic_core import PydanticCustomError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket, WebSocketState

from helpers import rules
from helpers.rules import Rule
from rcman.headers import SECURITY_HEADERS

log = logging.getLogger("rcman.problems")
MEDIA_TYPE = "application/problem+json"
# Validation error types of rcman's own.
RULE_REFUSED = "rule_refused"  # a helpers' rule refused the value
SETTINGS_NONE_SENT = "settings_none_sent"
STRING_EMPTY = "string_empty"  # string_too_short, where the shortest allowed is one
FIELD_INVALID = "field_invalid"  # any type FIELD_MESSAGES has no message for
PUBLIC_PATHS = {"/api/health", "/{path:path}"}  # the frontend's files need no identity

# Every problem rcman answers with: its words, and its HTTP status.
MESSAGES = {
    "not_authenticated": "Sign in first.",
    "not_allowed": "This account may not use rcman.",
    "admin_only": "Only administrators may do this.",
    "origin_refused": "The request did not come from rcman.",
    "not_found": "There is nothing here.",
    "method_not_allowed": "That is not allowed here.",
    "validation_failed": "Some of what was sent is not valid.",
    "body_unreadable": "What was sent could not be read as text.",
    "author_not_set": "Set your commit author name in Settings before committing.",
    "internal_error": "Something went wrong in rcman.",
    "too_many_reports": "Too many reports of failures just now. They are dropped.",
    "too_many_requests": "Too many requests just now. Try again in a moment.",
    # people
    "email_invalid": rules.EMAIL.sentence,
    "email_taken": "Someone with that address is there already.",
    "people_full": "rcman has room for no more people.",
    "person_not_found": "There is no such person.",
    # limits
    "memory_invalid": rules.MEMORY.sentence,
    "cpu_invalid": rules.CPU.sentence,
    "pids_invalid": rules.PIDS.sentence,
    "limit_below_use": (
        "A limit on memory or on processes cannot be set below what is in use now, because "
        "that would end work that is running. Set a higher limit, or stop what uses it first."
    ),
    "usage_unknown": (
        "rcman cannot tell what is in use, so it cannot lower this limit without risking "
        "running work. Set a higher limit, or none."
    ),
    "limits_not_applied": "rcman could not apply the limits, so nothing was changed.",
    # workspaces
    "workspace_not_found": "There is no such workspace.",
    "workspace_name_taken": "You have a workspace with that name already.",
    "workspace_not_ready": "The workspace is not ready: it is being made, or deleted, or failed.",
    "runtime_unavailable": "rcman cannot reach its containers just now. Try again in a moment.",
    "workspace_failed": "The workspace did not do what rcman asked of it.",
    "workspace_rebuilding": (
        "The workspace is being rebuilt, or rcman is working on its container. Try again once "
        "that is done."
    ),
    "rebuild_confirm_required": (
        "Rebuilding stops the workspace's server and terminal and makes its container again. "
        "Confirm that this is what you want."
    ),
    "rebuild_empty": "An empty workspace has no repository, so it has nothing to be rebuilt from.",
    "rebuild_no_container": (
        "The workspace's container is not there just now, so it cannot be rebuilt. rcman makes "
        "it again when it next starts."
    ),
    "rcman_starting": (
        "rcman is still bringing the workspaces back after it started. Try again in a moment."
    ),
    # servers
    "claude_not_installed": "Claude Code is not installed in the workspace: the approved "
    "version is not in the store, or the workspace cannot run it.",
    "claude_not_logged_in": "Connect your Claude account on the Claude page.",
    "claude_signing_out": "Your Claude account is being signed out: connect it again on the "
    "Claude page to start servers.",
    "claude_install_failed": "rcman could not get that Claude Code version.",
    "claude_checks_failed": "That Claude Code version failed the checks every version is held "
    "to before it is approved, so it was not approved. The checks it failed are below.",
    "claude_canary_failed": "rcman could not run the checks on that Claude Code version, so it "
    "was not approved and nothing was changed. Try again in a moment.",
    "claude_upgrade_busy": "Another Claude Code version is being rolled out. Try again when it "
    "is done.",
    # upgrading rcman
    "rcman_upgrade_refused": "rcman cannot be upgraded just now.",
    "rcman_upgrading": "rcman is being upgraded. Try again once the upgrade is done.",
    "login_failed": "Signing in to Claude failed. Get a new link and try again.",
    "no_login_in_progress": "No sign-in to Claude is waiting for a code. Get a new link.",
    "claude_unrecognised_output": "Claude Code answered with output rcman does not recognise.",
    "already_running": "A server is already running in this workspace.",
    "not_running": "No server is running in this workspace.",
    "not_stopped": "There is no server in this workspace that stopped and can be resumed.",
    "still_running": "Stop the server before forgetting it.",
    "unknown_server": "There is no server in this workspace.",
    "nothing_to_confirm": "The server is not asking anything.",
    "confirm_not_accepted": "The server did not accept the answer.",
    "timeout": "The workspace did not answer in time.",
    "prefix_invalid": rules.PREFIX.sentence,
    # git
    "git_not_installed": "git is not installed in the workspace.",
    "git_failed": "git refused. What it said is below.",
    "git_timeout": "git did not finish in time, so it was stopped.",
    "git_unrecognised_output": "git answered with output rcman does not recognise.",
    "not_a_repository": "This workspace is not a git repository: it has no .git of its own at "
    "its top.",
    "file_invalid": "A file name is not one rcman can stage or unstage.",
    "file_outside_repository": "A file is outside the repository.",
    "nothing_staged": "Nothing is staged, so there is nothing to commit.",
    "branch_invalid": "git does not take that as a branch name.",
    "branch_not_found": "There is no such branch.",
    "branch_name_taken": (
        "A local branch has the name a branch tracking this one would get, or a name "
        "git cannot keep beside it, so git cannot make it."
    ),
    "detached_head": "You are not on a branch. Switch to a branch first.",
    "no_push_remote": (
        "This branch tracks no remote branch, and the repository has no remote named "
        "origin to push it to."
    ),
    "no_commits_yet": "This branch has no commits yet. Commit something first.",
    "github_token_not_set": "Add a GitHub token in Settings before pushing to GitHub.",
    "diverged": (
        "This branch and the branch it tracks each have commits the other lacks, so it "
        "cannot simply be brought up to date. Merge or rebase it first."
    ),
    "upstream_gone": (
        "The branch this branch tracks is no longer on the remote, so there is nothing to "
        "pull from it."
    ),
}
# What a workspace that failed to be cloned is told, in plain words, beside what git said
# (workspaces.py): by the cause rcman recognised in it.
CLONE_MESSAGES = {
    "timeout": "Cloning took too long, so it was stopped.",
    "private_no_token": (
        "GitHub asked for a sign-in: the repository is private, or the address is wrong. "
        "If it is private, add a GitHub token in Settings and make the workspace again."
    ),
    "token_refused": (
        "GitHub did not accept your token for this repository. Check in Settings that the "
        "token is valid and may read it, then make the workspace again."
    ),
    "not_found": (
        "GitHub could not find this repository. Check the address; a private repository "
        "needs a GitHub token in Settings that may read it."
    ),
    "network": "rcman could not reach GitHub from the workspace.",
    "restarted": (
        "rcman restarted while this workspace was being made; delete it and make it again."
    ),
}
# The title of ``workspace_failed`` when deleting a person leaves a workspace of theirs that
# is still being made, or that could not be removed (workspaces.delete_person).
PERSON_DELETION_LEFT = (
    "A workspace of theirs is still being removed. They are let in nowhere meanwhile, and "
    "rcman finishes deleting them once it is gone, at the latest when rcman next starts."
)
# Why rolling out a Claude Code version could not get it, in plain words, by the kind of
# claude_release.ReleaseError: the problem's title when it is ``claude_install_failed``.
CLAUDE_INSTALL_MESSAGES = {
    "no_such_version": "Claude Code has no such version. Check the number.",
    "no_build": "That version has no build for the platform rcman runs on.",
    "checksum": (
        "What was downloaded did not match the checksum its release publishes, so it was "
        "thrown away."
    ),
    "too_big": "The download is bigger than rcman takes, so it was thrown away.",
    "network": (
        "rcman could not download it: the release site did not answer, or the download "
        "stopped part way. Try again."
    ),
}
# Why an upgrade of rcman cannot be asked for now, in plain words, by the reason
# rcman_upgrade.py gives: the title of ``rcman_upgrade_refused``, which the report of
# GET /api/rcman/upgrade gives too.
UPGRADE_REFUSED_MESSAGES = {
    "disabled": (
        "Upgrading rcman from the web interface is off. An administrator turns it on with the "
        "installer, from a shell."
    ),
    "units_unreadable": (
        "rcman cannot tell whether an upgrade could run: systemd did not answer as expected. "
        "Try again in a moment."
    ),
    "path_inactive": (
        "Nothing on the server is watching for a request to upgrade, so none would be acted on. "
        "Run the installer again from a shell."
    ),
    "under_way": "rcman is being upgraded already.",
    "rolling_out": (
        "A Claude Code version is being rolled out, or what one left is being cleared away. "
        "Try again when it is done."
    ),
    "creating": "A workspace is being made. Try again once it is made.",
    "rebuilding": "A workspace is being rebuilt. Try again once it is rebuilt.",
    "images": "rcman is clearing away images no workspace uses. Try again in a moment.",
}
# The status of ``claude_install_failed`` by kind: a release site that does not answer is a
# bad gateway, and the rest is for the administrator who named the version.
CLAUDE_INSTALL_STATUS = {
    "no_such_version": 409,
    "no_build": 409,
    "checksum": 409,
    "too_big": 409,
    "network": 502,
}
# What a workspace made from a repository's devcontainer.json is told when the file, or the
# image it names, is refused or fails (devcontainer.py, workspaces.py), in plain words: the
# key to remove or change, or what to do instead. {key}, {entry} and the like are filled in
# by what raises the message.
DEVCONTAINER_MESSAGES = {
    "refused_key": (
        'devcontainer.json asks for "{key}". {reason} '
        "Remove it from the file, push the change and make the workspace again."
    ),
    "refused_by_image": (
        'The image asks for "{key}" in its metadata entry "{entry}", which is the base image or '
        "a feature. {reason} Use an image or a feature that does not ask for it."
    ),
    "unknown_key": (
        'devcontainer.json has the key "{key}", which rcman does not know, so it cannot say '
        "what it would do. Remove it, push the change and make the workspace again."
    ),
    "unknown_key_in_image": (
        'The image\'s metadata entry "{entry}" has the key "{key}", which rcman does not '
        "know, so it cannot say what it would do. Use another image."
    ),
    "invalid_file": (
        "devcontainer.json is not valid JSON with comments: it goes wrong at line {line}, "
        "column {column}. Fix the file, push the change and make the workspace again."
    ),
    "not_an_object": (
        "devcontainer.json must hold one JSON object whose keys are the settings. "
        "Fix the file, push the change and make the workspace again."
    ),
    "too_deep": "devcontainer.json is nested too deeply to be read. Simplify the file.",
    "invalid_value": (
        "devcontainer.json holds a number that is far too big to be one of its settings. "
        "Fix the file, push the change and make the workspace again."
    ),
    "too_big": (
        "devcontainer.json is too big to be a configuration, so rcman did not read it. "
        "Make it smaller, push the change and make the workspace again."
    ),
    "symlink": (
        "devcontainer.json is a symbolic link, or is in a folder that is one, and rcman does "
        "not follow links there. Make it an ordinary file, push the change and make the "
        "workspace again."
    ),
    "unreadable": "rcman could not read devcontainer.json in the workspace.",
    "dockerfile_symlink": (
        "The Dockerfile devcontainer.json builds from is a symbolic link, or is in a folder that "
        "is one, and rcman does not follow links there. Make it an ordinary file, push the "
        "change and make the workspace again."
    ),
    "dockerfile_too_big": (
        "The Dockerfile devcontainer.json builds from is too big for rcman to read. Make it "
        "smaller, push the change and make the workspace again."
    ),
    "dockerfile_unreadable": (
        "rcman could not read the Dockerfile devcontainer.json builds from in the workspace."
    ),
    "no_image": (
        'devcontainer.json names no image and no Dockerfile. Name an image with "image", or a '
        'Dockerfile to build with "build"; features need one of them to be installed on.'
    ),
    "bad_value": (
        'devcontainer.json has a value of the wrong kind for "{key}": it must be {expected}. '
        "Fix the file, push the change and make the workspace again."
    ),
    "variable": (
        'devcontainer.json uses the variable "{variable}" in "{key}". It stands for a folder '
        "on the machine rcman runs on, which a workspace has none of. Use "
        '"containerWorkspaceFolder" for the work folder, push the change and make the '
        "workspace again."
    ),
    "env_reserved": (
        '"{where}" in {origin} sets "{name}", which rcman sets itself in every workspace or '
        "keeps for the git configuration it writes. "
        "Remove it from the file, push the change and make the workspace again."
    ),
    "user_unknown": (
        'The image has no user or group "{user}". Name a user that is in the image\'s '
        "passwd file, push the change and make the workspace again."
    ),
    "id_out_of_range": (
        'The user "{user}" has an id beyond the ids a workspace has. Name a user whose ids '
        "are within them."
    ),
    "metadata_invalid": (
        "The image's devcontainer.metadata label is not a list of settings objects, so "
        "rcman cannot tell what the image asks for. Use another image."
    ),
    "image_unreadable": 'rcman could not read the image "{image}": {said}',
    "image_unrunnable": (
        'rcman could not run a container from the image "{image}": {said} Use another image.'
    ),
    "image_gone": (
        'The image "{image}" is no longer available, so this workspace\'s container cannot '
        "be made again. Rebuild the workspace to make it from what its devcontainer.json asks "
        "for now, keeping its files, or delete it and make it again."
    ),
    "pull_failed": (
        'rcman could not pull the image "{image}": {said} Check the image name and tag; an '
        "image that needs a sign-in to its registry cannot be pulled."
    ),
    "git_unusable": (
        'The image "{image}" has no git that rcman can use: {said}. Use an image with a recent git.'
    ),
    "musl": (
        'The image "{image}" is built on musl, as Alpine images are, and Claude Code needs '
        "glibc. Use an image built on glibc, such as a Debian or Ubuntu one."
    ),
    "bad_value_in_image": (
        'The image\'s metadata entry "{entry}" has a value of the wrong kind for "{key}": it must '
        "be {expected}. Use another image."
    ),
    "build_image_refused": (
        "The image the build made was not accepted: {reason}. It was not kept."
    ),
    "image_labels": (
        'The image "{image}" carries the label "{label}", which rcman uses for its own '
        "containers, and a container takes its image's labels. Use another image."
    ),
    "runtime_gone": (
        "rcman could not reach its containers while it was making this workspace, so the "
        "workspace was not finished. Delete it and make it again."
    ),
    "default_image_gone": (
        "The image this workspace was made from is no longer in rcman's storage, so the "
        "workspace's container cannot be made again. Rebuild the workspace to make it from "
        "what its devcontainer.json asks for now, keeping its files, or delete it and make it "
        "again."
    ),
    "built_image_gone": (
        "The image rcman built for this workspace is no longer in its storage, so the "
        "workspace's container cannot be made again. Rebuild the workspace to build it again, "
        "keeping its files, or delete it and make it again."
    ),
    "build_registry_unreachable": (
        "The build could not reach the network: a registry or an address it needs did not "
        "answer. Try again later; the end of what the builder said is below.\n{tail}"
    ),
    "build_image_missing": (
        "The build could not find an image it needs: the image that devcontainer.json or its "
        "Dockerfile starts from, or one of its features, is not there. Check the name and tag; "
        "the end of what the builder said is below.\n{tail}"
    ),
    "build_sign_in": (
        "A registry refused the build. Either an image or a feature the build needs is not there "
        "or it is private: rcman cannot sign in to a registry, so only what can be pulled "
        "without signing in can be built on. The end of what the builder said is below.\n{tail}"
    ),
    "build_step_failed": (
        "A step of the Dockerfile failed, so the image could not be built. Fix the step, push "
        "the change and make the workspace again. The end of what the builder said is "
        "below.\n{tail}"
    ),
    "build_feature_failed": (
        'The feature "{feature}" failed to install, so the image could not be built. Remove it '
        "or fix it, push the change and make the workspace again. The end of what the builder "
        "said is below.\n{tail}"
    ),
    "build_no_space": (
        "The builder ran out of disk space. Ask an administrator to free space on the machine "
        "rcman runs on, or build less. The end of what the builder said is below.\n{tail}"
    ),
    "build_context_too_big": (
        "The repository is bigger than rcman copies into a builder, so the image was not built. "
        "Keep what the build does not need out of the repository, or out of the folder it is "
        "built from."
    ),
    "build_image_too_big": (
        "The image that was built is bigger than rcman copies out of its builder, so it was "
        "not kept. Build a smaller image."
    ),
    "build_timeout": (
        "Building the image took too long, so it was stopped. Make the build shorter, or build "
        "on an image that has more of what it needs."
    ),
    "build_failed": (
        "rcman could not build the image devcontainer.json asks for. The end of what the "
        "builder said is below.\n{tail}"
    ),
    "hook_failed": (
        "{command} failed, exit code {code}. What it printed last is below. The workspace "
        "was kept: rebuild it to run its commands again, keeping its files, or delete it to "
        "remove it."
    ),
    "hook_timeout": (
        "{command} took too long, so it was stopped. What it printed last is below. The "
        "workspace was kept: rebuild it to run its commands again, keeping its files, or "
        "delete it to remove it."
    ),
}
# The advice a refusal of DEVCONTAINER_MESSAGES gives to a workspace being made, and what it
# says in its place to one being rebuilt, whose file is read from its own work folder.
MAKE_AGAIN_ADVICE = "push the change and make the workspace again"
REBUILD_ADVICE = "then rebuild the workspace"


def rebuild_advice(message: str) -> str:
    """``message``, a refusal of DEVCONTAINER_MESSAGES, as a workspace being rebuilt is told it:
    to change its file and rebuild it, not to make it again."""
    return message.replace(f", {MAKE_AGAIN_ADVICE}", f", {REBUILD_ADVICE}")


# What a workspace whose rebuild did not finish is told (workspaces.py), in plain words:
# ``rebuild_error`` in its listing. {cause} is why it failed, in the words of
# DEVCONTAINER_MESSAGES or of a lifecycle command that failed.
REBUILD_MESSAGES = {
    "restarted": (
        "rcman restarted while this workspace was being rebuilt, so the rebuild was stopped "
        "and the workspace was left as it was. Rebuild it again."
    ),
    "restarted_switching": (
        "rcman restarted while this workspace's container was being replaced, so the "
        "container was made again as it was before the rebuild and its create commands were "
        "run again. Rebuild it again."
    ),
    "rolled_back": (
        "The rebuild failed, so the workspace was put back as it was: its container was made "
        "again from the image it had, and its create commands were run again. What the new "
        "commands wrote to the work folder stays. Why it failed is below.\n{cause}"
    ),
    "hook_failed": "{command} failed, exit code {code}. What it printed last is below.",
    "hook_timeout": ("{command} took too long, so it was stopped. What it printed last is below."),
    "rollback_failed": (
        "The rebuild failed, and the workspace could not be put back as it was at once: rcman "
        "puts its container back as soon as it can, trying again every little while, and until "
        "then its server, git and terminal are not available. Why it failed is below.\n{cause}"
    ),
    "put_back_later": (
        "The rebuild failed, and the workspace could not be put back as it was at once, so it "
        "was put back afterwards: its container was made again from the image it had, and its "
        "create commands were run again. What the new commands wrote to the work folder stays. "
        "Why it failed is below.\n{cause}"
    ),
}
STATUS = {
    "not_authenticated": 401,
    "not_allowed": 403,
    "admin_only": 403,
    "origin_refused": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "validation_failed": 422,
    "body_unreadable": 400,
    "author_not_set": 409,
    "internal_error": 500,
    "too_many_reports": 429,
    "too_many_requests": 429,
    "rcman_upgrade_refused": 409,
    "rcman_upgrading": 409,
    "email_invalid": 400,
    "email_taken": 409,
    "people_full": 409,
    "person_not_found": 404,
    "memory_invalid": 400,
    "cpu_invalid": 400,
    "pids_invalid": 400,
    "limit_below_use": 409,
    "usage_unknown": 409,
    "limits_not_applied": 502,
    "workspace_not_found": 404,
    "workspace_name_taken": 409,
    "workspace_not_ready": 409,
    "runtime_unavailable": 503,
    "workspace_failed": 502,
    "workspace_rebuilding": 409,
    "rebuild_confirm_required": 422,
    "rebuild_empty": 409,
    "rebuild_no_container": 409,
    "rcman_starting": 409,
    "claude_not_installed": 409,
    "claude_install_failed": 409,
    "claude_checks_failed": 409,
    "claude_canary_failed": 502,
    "claude_upgrade_busy": 409,
    "claude_not_logged_in": 409,
    "claude_signing_out": 409,
    "login_failed": 409,
    "no_login_in_progress": 409,
    "claude_unrecognised_output": 502,
    "already_running": 409,
    "not_running": 409,
    "not_stopped": 409,
    "still_running": 409,
    "unknown_server": 404,
    "nothing_to_confirm": 409,
    "confirm_not_accepted": 409,
    "timeout": 504,
    "prefix_invalid": 400,
    "git_not_installed": 409,
    "git_failed": 409,
    "git_timeout": 504,
    "git_unrecognised_output": 502,
    "not_a_repository": 409,
    "file_invalid": 400,
    "file_outside_repository": 400,
    "nothing_staged": 409,
    "branch_invalid": 400,
    "branch_not_found": 404,
    "branch_name_taken": 409,
    "detached_head": 409,
    "no_push_remote": 409,
    "no_commits_yet": 409,
    "github_token_not_set": 409,
    "diverged": 409,
    "upstream_gone": 409,
}


class Problem(Exception):
    def __init__(
        self,
        code: str,
        status: int | None = None,
        title: str | None = None,
        headers: dict[str, str] | None = None,
        **extra,
    ):
        super().__init__(code)
        self.code = code
        self.status = status or STATUS[code]
        self.title = title or MESSAGES[code]
        self.headers = headers or {}
        self.extra = extra


def body(code: str, status: int, title: str, **extra) -> dict:
    """A problem's details, as a response carries them or an answer reports one."""
    return {
        "type": f"urn:rcman:problem:{code}",
        "title": title,
        "status": status,
        "code": code,
        **extra,
    }


def response(code: str, status: int, title: str, **extra) -> JSONResponse:
    return JSONResponse(
        body(code, status, title, **extra), status_code=status, media_type=MEDIA_TYPE
    )


# What a refused field is told, by the type of the validation error, with the
# numbers from the error's own context, so they are the field's constraints.
# The library's own message is never sent.
FIELD_MESSAGES = {
    RULE_REFUSED: "{sentence}",
    "missing": "This is required.",
    "string_type": "This must be text.",
    "string_unicode": "This holds half of a character, which is not text.",
    STRING_EMPTY: "This cannot be empty.",
    "string_too_long": "This can be at most {max_length} characters long.",
    "list_type": "This must be a list.",
    "too_short": "This must hold at least {min_length} items.",
    "too_long": "This can hold at most {max_length} items.",
    "bool_type": "This must be true or false.",
    "bool_parsing": "This must be true or false.",
    "int_type": "This must be a whole number.",
    "int_parsing": "This must be a whole number.",
    "greater_than_equal": "This must be at least {ge}.",
    "less_than_equal": "This must be at most {le}.",
    "literal_error": "This must be one of {expected}.",
    "json_invalid": "What was sent is not valid JSON.",
    "model_attributes_type": "What was sent must be a JSON object.",
    SETTINGS_NONE_SENT: "Send at least one setting.",
    FIELD_INVALID: "This is not valid.",
}


# What a ruled field refuses with its rule's sentence: what the rule refuses, any
# length the field is given besides, and text holding half of a character.
_RULED = ("string_pattern_mismatch", "string_too_short", "string_too_long", "string_unicode")


def ruled(rule: Rule, **constraints):
    """Text that must match a helpers' rule, and any length ``constraints``.

    The rule's anchored pattern is validated and published; a value it or a
    length refuses is refused with the rule's sentence.
    """

    def explain(value, handler):
        try:
            return handler(value)
        except ValidationError as error:
            if all(e["type"] in _RULED for e in error.errors()):
                raise PydanticCustomError(
                    RULE_REFUSED, "{sentence}", {"sentence": rule.sentence}
                ) from None
            raise

    return Annotated[str, Field(pattern=rule.anchored, **constraints), WrapValidator(explain)]


def _field(error) -> str:
    """The field an error is about: its location without the leading part (body, query, ...).

    A body that is not JSON is about the request as a whole, not the place it broke.
    """
    loc = error["loc"]
    if error["type"] == "json_invalid":
        return "request"
    parts = (
        list(loc[1:])
        if loc and loc[0] in ("body", "query", "path", "header", "cookie")
        else list(loc)
    )
    return ".".join(str(p) for p in parts) or "request"


def _message(error) -> str:
    """What a field is told about an error, in rcman's words."""
    kind, context = error["type"], error.get("ctx") or {}
    if kind == "string_too_short" and context.get("min_length") == 1:
        kind = STRING_EMPTY
    if kind not in FIELD_MESSAGES:
        log.warning("validation error type %s has no message in FIELD_MESSAGES", kind)
        kind = FIELD_INVALID
    try:
        return FIELD_MESSAGES[kind].format(**context)
    except (KeyError, IndexError):
        log.warning("validation error type %s lacks the context its message needs", kind)
        return FIELD_MESSAGES[FIELD_INVALID]


# A body FastAPI cannot read as text is its 400; rcman sends no 400 of its own.
HTTP_CODES = {400: "body_unreadable", 404: "not_found", 405: "method_not_allowed"}


def code_of(exc: Exception) -> str:
    """The code of the problem an exception is answered with."""
    if isinstance(exc, Problem):
        return exc.code
    if isinstance(exc, (RequestValidationError, WebSocketRequestValidationError)):
        return "validation_failed"
    if isinstance(exc, StarletteHTTPException):
        return HTTP_CODES.get(exc.status_code, "internal_error")
    return "internal_error"


# A WebSocket closed with a problem once it was accepted gets this plus the problem's status:
# its page reads the problem from the frame sent before (routers/terminal.py).
CLOSE_PROBLEM = 4000


async def answer(connection: HTTPConnection, answered: JSONResponse) -> JSONResponse | None:
    """``answered``: for a request, and for a WebSocket's upgrade before it is accepted, which
    Starlette sends as the upgrade's refusal over HTTP. A WebSocket refused once it was accepted
    is closed with CLOSE_PROBLEM and the status instead, as no answer can be sent then."""
    if isinstance(connection, WebSocket) and (
        connection.application_state != WebSocketState.CONNECTING
    ):
        if connection.application_state == WebSocketState.CONNECTED:
            await connection.close(CLOSE_PROBLEM + answered.status_code)
        return None
    return answered


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(request: HTTPConnection, exc: Problem):
        answered = response(exc.code, exc.status, exc.title, **exc.extra)
        answered.headers.update(exc.headers)
        return await answer(request, answered)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(WebSocketRequestValidationError)
    async def _validation(request: HTTPConnection, exc: RequestValidationError):
        errors = [{"field": _field(e), "message": _message(e)} for e in exc.errors()]
        code = code_of(exc)
        return await answer(request, response(code, STATUS[code], MESSAGES[code], errors=errors))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = code_of(exc)
        return response(code, exc.status_code, MESSAGES[code])

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        code = code_of(exc)
        answer = response(code, STATUS[code], MESSAGES[code])
        # Outside the middleware that adds them to the rest (Starlette answers this one itself).
        answer.headers.update(SECURITY_HEADERS)
        return answer
