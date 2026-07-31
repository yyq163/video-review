from __future__ import annotations

from dataclasses import dataclass

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from compliance.generate_compliance_artifacts import _resolved_python_names


@dataclass
class FakeDistribution:
    requires: list[str] | None


def test_python_dependency_closure_terminates_on_cycle_and_deduplicates() -> None:
    installed = {
        canonicalize_name("root"): FakeDistribution(["shared", "extra-provider[feature]"]),
        canonicalize_name("shared"): FakeDistribution(["root"]),
        canonicalize_name("extra-provider"): FakeDistribution(
            ["shared", "feature-leaf; extra == 'feature'"]
        ),
        canonicalize_name("feature-leaf"): FakeDistribution([]),
    }

    result = _resolved_python_names(installed, [Requirement("root")])

    assert result == ["extra-provider", "feature-leaf", "root", "shared"]


def test_python_dependency_closure_reports_missing_distribution() -> None:
    try:
        _resolved_python_names({}, [Requirement("missing==1.0")])
    except RuntimeError as exc:
        assert str(exc) == "required Python distribution is not installed: missing"
    else:
        raise AssertionError("missing dependency must fail artifact generation")
