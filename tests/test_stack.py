"""tests/container/stack.py's global options for Caddy, on the machine."""

from tests.container import stack as st


def test_the_stacks_global_options_ask_what_they_are_given_or_nothing():
    """The gate that shows what Caddy does without the installer's on_demand_tls runs the
    stack's Caddy with no ``on_demand_tls``; every other start asks the address given."""
    block = "\ton_demand_tls {\n\t\task http://127.0.0.1:1/.rcman/ask\n\t}\n"
    asking = st.global_options("http://127.0.0.1:1/.rcman/ask")
    assert block in asking
    assert st.global_options() == st.global_options(st.ASK_URL)
    bare = st.global_options("")
    assert "on_demand_tls" not in bare and "ask" not in bare
    assert bare == asking.replace(block, "")
