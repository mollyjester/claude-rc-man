"""Collection rules shared by the whole suite."""

import os

# tests/container/ runs as root inside the test container, started by
# tests/test_container_suite.py; elsewhere it is not collected at all.
collect_ignore = [] if os.environ.get("RCMAN_IN_CONTAINER") == "1" else ["container"]
