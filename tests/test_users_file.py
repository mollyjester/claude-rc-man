"""users.toml: the one list of people, and the allowlist generated from it."""

import pytest

from helpers import users_file as uf

ALICE = uf.User(email="alice@example.com", linux="alice", admin=True)
BOB = uf.User(email="bob@example.com", linux="bob", admin=False)


def test_missing_file_is_an_empty_list(tmp_path):
    assert uf.load(tmp_path / "users.toml") == []


def test_write_then_load_round_trips(tmp_path):
    path = tmp_path / "users.toml"
    uf.save(path, [BOB, ALICE])
    assert uf.load(path) == [ALICE, BOB]  # stored sorted by Linux name


def test_written_file_says_how_it_is_managed(tmp_path):
    path = tmp_path / "users.toml"
    uf.save(path, [ALICE])
    assert path.read_text(encoding="utf-8").startswith("# ")


def test_emails_are_stored_lowercase(tmp_path):
    path = tmp_path / "users.toml"
    uf.save(path, [uf.User(email="Alice@Example.COM", linux="alice", admin=False)])
    assert uf.load(path)[0].email == "alice@example.com"


@pytest.mark.parametrize(
    "text,problem",
    [
        ('[[user]]\nemail = "a@example.com"\nlinux = "a"\n', "admin"),
        ('[[user]]\nemail = "not-an-email"\nlinux = "a"\nadmin = false\n', "email"),
        ('[[user]]\nemail = "a@example.com"\nlinux = "Root!"\nadmin = false\n', "linux"),
        ('[[user]]\nemail = "a@example.com"\nlinux = "a"\nadmin = "yes"\n', "admin"),
        (
            '[[user]]\nemail = "a@example.com"\nlinux = "a"\nadmin = false\n'
            '[[user]]\nemail = "A@example.com"\nlinux = "b"\nadmin = false\n',
            "duplicate email",
        ),
        (
            '[[user]]\nemail = "a@example.com"\nlinux = "a"\nadmin = false\n'
            '[[user]]\nemail = "b@example.com"\nlinux = "a"\nadmin = false\n',
            "duplicate linux",
        ),
        ('[[user]]\nemail = "a@example.com"\nlinux = "a"\nadmin = false\nextra = 1\n', "extra"),
        ("not toml at all [", "parse"),
    ],
)
def test_invalid_files_are_refused_with_the_problem(tmp_path, text, problem):
    path = tmp_path / "users.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(uf.UsersFileError, match=problem):
        uf.load(path)


def test_save_refuses_what_load_would_refuse(tmp_path):
    with pytest.raises(uf.UsersFileError):
        uf.save(
            tmp_path / "users.toml",
            [ALICE, uf.User(email="x@example.com", linux="alice", admin=False)],
        )
    assert not (tmp_path / "users.toml").exists()


@pytest.mark.parametrize("name", ["alice", "a", "a1", "a_b", "a-b", "x" * 32])
def test_valid_linux_names(name):
    assert uf.valid_linux(name)


@pytest.mark.parametrize("name", ["", "Alice", "1a", "-a", "a b", "a.b", "x" * 33, "a$"])
def test_invalid_linux_names(name):
    assert not uf.valid_linux(name)


@pytest.mark.parametrize("email", ["a@example.com", "first.last+tag@sub.example.org"])
def test_valid_emails(email):
    assert uf.valid_email(email)


@pytest.mark.parametrize("email", ["", "a", "a@b", "a@@b.c", "a b@c.d", 'a"@b.c', "a@b.c\n"])
def test_invalid_emails(email):
    assert not uf.valid_email(email)


def test_allowlist_is_one_email_per_line_matching_the_users(tmp_path):
    text = uf.allowlist([BOB, ALICE])
    assert text.splitlines() == ["alice@example.com", "bob@example.com"]
    assert text.endswith("\n")


def test_empty_allowlist_is_empty():
    assert uf.allowlist([]) == ""


def test_by_email_and_by_linux(tmp_path):
    users = [ALICE, BOB]
    assert uf.by_email(users, "ALICE@example.com") == ALICE
    assert uf.by_linux(users, "bob") == BOB
    assert uf.by_email(users, "nobody@example.com") is None


def test_a_linux_name_is_as_long_as_useradd_allows():
    """useradd refuses a name longer than 32 characters."""
    assert uf.MAX_LINUX_NAME_LENGTH == 32
    assert uf.LINUX_NAME.fullmatch("a" * uf.MAX_LINUX_NAME_LENGTH)
    assert not uf.LINUX_NAME.fullmatch("a" * (uf.MAX_LINUX_NAME_LENGTH + 1))
