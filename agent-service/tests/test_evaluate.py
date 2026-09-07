import importlib.util
from pathlib import Path

from agent_service.language import LanguageError, Proposal

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/evaluate.py"
SPEC = importlib.util.spec_from_file_location("live_evaluate", SCRIPT)
evaluate_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluate_module)


async def test_provider_failure_does_not_abort_sample_and_is_reported_separately():
    sample = [
        {
            "category": "fixture",
            "message": f"case {number}",
            "expected": {"intent": "other"},
        }
        for number in range(25)
    ]

    class OccasionallyFailingAdapter:
        def __init__(self):
            self.calls = 0

        async def extract(self, *_args):
            self.calls += 1
            if self.calls == 7:
                raise LanguageError("language_unavailable", 503)
            if self.calls == 9:
                return Proposal(
                    intent="quote",
                    candidates=[
                        {"field": "idade", "value": "35", "evidence": "case 9"}
                    ],
                    ambiguous=[],
                    refresh=False,
                )
            intent = "quote" if self.calls == 13 else "other"
            return Proposal(intent=intent, candidates=[], ambiguous=[], refresh=False)

    adapter = OccasionallyFailingAdapter()
    quality, failures = await evaluate_module.evaluate_cases(adapter, sample, [])

    assert adapter.calls == 25
    assert failures == {
        "total": 2,
        "by_code": {"language_unavailable": 1, "language_invalid_grounding": 1},
        "by_http_status": {"503": 1},
    }
    assert quality["evaluated"] == 23
    assert quality["passed"] == 22
    assert quality["failed"] == 1
    assert quality["by_category"] == {
        "fixture": {"passed": 22, "failed": 1, "evaluated": 23}
    }
