"""Every graded case, replayed through the MCP server, gets the same verdict as the direct run.

The agent's proposals, the approver's decisions and the admin's reverts all go through MCP tool calls with a key
for each role, and the same grader grades the result (see `gwp.evals.mcp_runner`).
"""

import logging

import pytest

from gwp.evals.cases import load_cases
from gwp.evals.mcp_runner import compare

pytestmark = pytest.mark.eval
logging.getLogger("strands").setLevel(logging.CRITICAL)

CASES = load_cases()
PREDICTED = {c.id for c in CASES if c.predicted.get("adversarial_system_success")}


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_cooperative_script_through_mcp_passes_like_the_direct_run(case):
    direct, via, p = compare(case, "cooperative")
    assert via.verdict == "success" == direct.verdict, via.mismatches
    assert p.same, (direct.verdict, via.verdict, via.mismatches)
    assert p.denied_calls == 0  # no call is denied; L01 makes none, since the reader fails before the run parks
    assert p.access_records == p.tool_calls  # the server recorded every call the client made


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_adversarial_script_through_mcp_gets_through_only_where_predicted(case):
    direct, via, p = compare(case, "adversarial")
    assert p.same, (direct.verdict, via.verdict, via.mismatches)
    assert p.access_records == p.tool_calls
    if case.id in PREDICTED:
        assert via.verdict == "unsafe" and via.injection_system
    else:
        assert via.verdict != "unsafe", via.mismatches
        assert not via.injection_system
