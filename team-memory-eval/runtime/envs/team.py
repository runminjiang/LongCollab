"""单次成员回合的动作、成员状态与错误类型。"""

from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, RootModel
from pydantic.json_schema import SkipJsonSchema


class ActionFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    to: str = ""
    files: list[str] = Field(default_factory=list)
    ready: bool = False
    submissions: dict[str, str] = Field(default_factory=dict)


class MessageAction(ActionFields):
    kind: Literal["handoff"]
    message: str = Field(min_length=1, validation_alias=AliasChoices("message", "text"))


class ControlAction(ActionFields):
    kind: Literal["ready"]
    # 旧动作快照在无需正文时也会写入 text。
    text: SkipJsonSchema[str] = Field(default="", exclude=True)


class Action(
    RootModel[
        Annotated[
            MessageAction | ControlAction,
            Field(discriminator="kind"),
        ]
    ]
):
    """按 kind 选择本次动作的正文字段。"""


@dataclass
class Member:
    id: str
    role: str
    assignment: str
    resource_ids: list
    category: str = "worker"
    session_id: str | None = None
    started: bool = False
    ready: bool = False
    profile: str = ""
    budget_exhausted: bool = False


@dataclass
class State:
    members: dict = field(default_factory=dict)
    initial: list = field(default_factory=list)
    next: str | None = None
    reason: str = ""
    status: str = "new"
    detail: str = ""
    turns: int = 0
    inflight: str = ""


class HarnessError(RuntimeError):
    pass


class InvalidAction(ValueError):
    """成员可以在原会话中纠正的动作错误。"""
