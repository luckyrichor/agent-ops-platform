from pathlib import Path

from agent_ops.evals.support_runner import evaluate


async def test_labeled_support_decisions() -> None:
    result = await evaluate(Path("evals/support-v1.json"))
    assert result["passed"] == result["cases"] == 47
