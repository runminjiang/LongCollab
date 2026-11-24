"""规划是单次结构化请求，角色权限由任务校验。"""

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Identifier
    role: Text
    assignment: Text
    resource_ids: list[str]


class TeamPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    members: list[Assignment] = Field(min_length=1)
    first_member: Identifier

    @model_validator(mode="after")
    def check_ids(self):
        ids = [m.id for m in self.members]
        if len(set(ids)) != len(ids) or self.first_member not in ids:
            raise ValueError("首发或重复成员 ID 无效")
        return self


def plan(task, complete):
    mode = "generated" if task.benchmark == "drbench" else "fixed"
    public = task.public()
    schema = TeamPlan.model_json_schema()
    if mode == "fixed":
        if not task.roles:
            raise ValueError("此任务没有预设团队，请使用 generated 分工")
        public["fixed_roles"] = task.roles
        seats = []
        for role in task.roles:
            seats.append(
                {
                    "type": "object",
                    "properties": {
                        "id": {"const": role["id"]},
                        "role": {"const": role["role"]},
                        "assignment": {"type": "string", "minLength": 1},
                        "resource_ids": {"const": role["resource_ids"]},
                    },
                    "required": ["id", "role", "assignment", "resource_ids"],
                    "additionalProperties": False,
                }
            )
        schema["properties"]["members"] = {
            "type": "array",
            "prefixItems": seats,
            "items": False,
            "minItems": len(seats),
            "maxItems": len(seats),
        }
        schema["properties"]["first_member"] = {
            "type": "string",
            "enum": [r["id"] for r in task.roles],
        }
        rule = (
            "For EACH of these IDs write one assignment, in the listed order: "
            + ", ".join(r["id"] for r in task.roles)
            + ". Copy their IDs, role names and resource IDs exactly."
        )
    elif mode == "generated":
        rule = "Design a small expert team. Choose its size, member IDs, roles, tasks and authorized resources."
        if task.benchmark == "drbench":
            schema["properties"]["members"]["minItems"] = 2
            schema["$defs"]["Assignment"]["properties"]["resource_ids"] = {"const": ["drbench_app"]}
            rule += (
                " Choose at least two complementary roles for this specific business question. "
                "Grant each worker the supplied drbench_app resource and assign distinct evidence-gathering "
                "or synthesis responsibilities. "
                "The business persona is the report's audience."
            )
    else:
        raise ValueError("未知分工模式")
    prompt = (
        "Make one team plan. "
        + rule
        + (
            " Include in the assignments who assembles, uploads, and submits each "
            "named final output. Assign concrete contributions others can use. Select only the first speaker. Return JSON only.\n"
        )
        + json.dumps({"task": public, "schema": schema}, ensure_ascii=False)
    )
    raw = complete(prompt).strip()
    if raw.startswith("```") and raw.endswith("```") and "\n" in raw:
        raw = raw.split("\n", 1)[1][:-3].strip()
    result = TeamPlan.model_validate_json(raw)
    if mode == "generated" and task.benchmark == "drbench":
        if len(result.members) < 2:
            raise ValueError("DRBench 团队至少需要两名成员")
        if len({a.role.casefold() for a in result.members}) != len(result.members):
            raise ValueError("DRBench 规划需要互不重复的岗位")
    roles = {m["id"]: m for m in task.roles}
    if mode == "fixed" and set(roles) != {a.id for a in result.members}:
        raise ValueError("分工未覆盖固定成员")
    for a in result.members:
        if task.benchmark == "drbench" and "drbench_app" not in a.resource_ids:
            raise ValueError("DRBench 工作成员需要 drbench_app 工具权限")
        if (
            len(set(a.resource_ids)) != len(a.resource_ids)
            or not set(a.resource_ids) <= task.resources.keys()
        ):
            raise ValueError("资源授权无效")
        if mode == "fixed" and (
            a.role != roles[a.id]["role"]
            or set(a.resource_ids) != set(roles[a.id]["resource_ids"])
        ):
            raise ValueError("固定角色或授权被改变")
    return result
