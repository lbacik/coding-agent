from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class JUnitResult:
    """One command's evidence, reduced to what the MVP contract needs: how
    many tests actually executed, the identifier of every individual
    failure, and the identifier of every individual pass.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (the runtime
    owns the evidence; JUnit parsing returns the ids of passing tests;
    Criterion Evidence records the identifier and result of every test that
    executed).

    MVP row ids: L2-2.
    """

    executed: int
    failure_ids: frozenset[str]
    passed_ids: frozenset[str] = frozenset()


class MalformedJUnitReport(ValueError):
    """The evidence file did not parse as JUnit XML (contract §7's only
    v1 evidence format)."""


def _case_identifier(case: ET.Element) -> str:
    classname = case.get("classname", "")
    name = case.get("name", "")
    return f"{classname}::{name}" if classname else name


def parse_junit_xml(text: str) -> JUnitResult:
    """Parse a JUnit XML report into an executed-test count, the identifier
    of every individual failure, and the identifier of every individual
    pass (contract §9; MVP row L2-2).

    Accepts both a bare `<testsuite>` root and a `<testsuites>` wrapper,
    since pytest, PHPUnit and vitest do not agree on which one they emit —
    `junit-xml` is one format name covering slightly different shapes, and
    this is the one place that difference is absorbed.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise MalformedJUnitReport(str(exc)) from exc

    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.iter("testsuite"))
    else:
        raise MalformedJUnitReport(f"unexpected root element <{root.tag}>")

    executed = 0
    failure_ids: set[str] = set()
    passed_ids: set[str] = set()
    for suite in suites:
        for case in suite.findall("testcase"):
            if case.find("skipped") is not None:
                continue
            executed += 1
            if case.find("failure") is not None or case.find("error") is not None:
                failure_ids.add(_case_identifier(case))
            else:
                passed_ids.add(_case_identifier(case))
    return JUnitResult(
        executed=executed,
        failure_ids=frozenset(failure_ids),
        passed_ids=frozenset(passed_ids),
    )
