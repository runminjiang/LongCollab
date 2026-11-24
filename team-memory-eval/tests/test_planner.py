import json

import pytest

from runtime.planner import plan
from tests.fakes import fixed, sample_task


def test_one_request_and_fixed_roster():
    task = sample_task()
    calls = []

    def complete(prompt):
        calls.append(prompt)
        return json.dumps(fixed(task))

    assert plan(task, complete).first_member == "A"
    assert len(calls) == 1
    one = fixed(task)
    one["members"].pop()
    with pytest.raises(ValueError):
        plan(task, lambda _: json.dumps(one))


@pytest.mark.parametrize("change", ["id", "resource_ids", "role", "first"])
def test_invalid_plan(change):
    task = sample_task()
    p = fixed(task)
    if change == "first":
        p["first_member"] = "missing"
    else:
        p["members"][0][change] = {
            "id": "../A",
            "resource_ids": ["private"],
            "role": "X",
        }[change]
    with pytest.raises(ValueError):
        plan(task, lambda _: json.dumps(p))
