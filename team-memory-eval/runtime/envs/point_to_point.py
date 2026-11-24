"""每次运行只唤醒一名成员，由其交棒给下一位。"""

import asyncio
import json
import random
from dataclasses import asdict
from uuid import uuid4

from runtime.envs.team import Action, HarnessError, InvalidAction, Member, State
from runtime.planner import plan, roster_mode
from service import archive, budget, messages
from service.events import emit
from service.store import encode, write_json


class PointToPointEnv:
    actions = {
        "handoff": "Send a message and optional uploaded files to one other worker. Use their exact worker ID. "
        'Example: {"kind":"handoff","to":"WORKER_ID","message":"Please review the attached draft.",'
        '"files":["UPLOADED_FILE_ID"],"ready":true}. Use ready=true when your own work is complete.',
        "ready": "End your own assignment. Use submissions for final files.",
    }

    def __init__(
        self,
        store,
        sandbox,
        complete,
        *,
        max_turns=40,
        max_team_tokens=None,
        max_agent_tokens=None,
    ):
        self.store, self.sandbox, self.complete = store, sandbox, complete
        self.max_turns, self.max_agent_tokens = max_turns, max_agent_tokens
        self.max_team_tokens = (
            250000
            if max_team_tokens is None and max_agent_tokens is None
            else max_team_tokens
        )
        self.lock = asyncio.Lock()

    def _state(self, db):
        data = json.loads(
            db.execute("SELECT data FROM state WHERE key='team'").fetchone()[0]
        )
        data["members"] = {k: Member(**v) for k, v in data["members"].items()}
        return State(**data)

    def save(self, db, state):
        db.execute(
            "INSERT OR REPLACE INTO state VALUES('team',?)", (encode(asdict(state)),)
        )

    def start(self, task, seed):
        self.task, self.seed = task, seed
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM state").fetchone():
                raise ValueError("运行目录已使用")
            state = State(status="planning")
            self.save(db, state)
            emit(db, "reset", task=task.id, seed=seed)
            emit(db, "planner_called", mode=roster_mode(task.benchmark))
        try:

            def request_plan(prompt):
                answer = self.complete(prompt)
                with self.store.transaction() as db:
                    emit(db, "planner_response", text=answer)
                return answer

            planned = plan(task, request_plan)
        except budget.Exhausted as error:
            self.fail("budget_exhausted", error)
            return
        except Exception as error:  # noqa: BLE001 — 规划端点的任意失败均结束此次单次分工
            self.fail("planning_failed", error)
            return
        with self.store.transaction() as db:
            state.members = {a.id: Member(**a.model_dump()) for a in planned.members}
            for spec in task.roles:
                state.members[spec["id"]].profile = spec.get("description", "")
            state.initial = list(state.members)
            state.next, state.reason = planned.first_member, "planner_first"
            state.status = "running"
            self.save(db, state)
            emit(db, "planned", plan=planned.model_dump())
        write_json(self.store.directory / "plan.json", planned.model_dump())
        for member in state.members.values():
            self.sandbox.prepare(member)

    def fail(self, status, error):
        with self.store.transaction() as db:
            state = self._state(db)
            state.status, state.detail, state.inflight = status, str(error), ""
            self.save(db, state)
            emit(db, "stopped", status=status, error=str(error))

    def public_input(self, state, member, mail):
        delta = {
            "member": member.id,
            "role_type": member.category,
            "actions": self.actions,
            "mail": mail,
            "wake_reason": state.reason,
        }
        unavailable = [m.id for m in state.members.values() if m.budget_exhausted]
        if unavailable:
            delta["unavailable_members"] = unavailable
        if member.started:
            return delta
        roster = [
            {
                "id": m.id,
                "role": m.role,
                "description": m.profile,
                "assignment": m.assignment,
            }
            for m in state.members.values()
        ]
        public = self.task.public()
        public["resources"] = {k: self.task.resources[k] for k in member.resource_ids}
        return {
            "task": public,
            "roster": roster,
            **delta,
            "role": member.role,
            "profile": member.profile,
            "assignment": member.assignment,
            "instructions": "Team CLI: python -m tools.client list | search QUERY | read NAME [DEST] | call NAME JSON | "
            "upload PATH | download ID DEST. Materials listed by the service live on the host: use "
            "python -m tools.client read NAME to obtain their text. /work contains only your own files. "
            "Upload returns an id. To deliver a file first upload it, then "
            'end with e.g. {"kind":"ready","submissions":{"OUTPUT_SLOT":"uploaded_id"}}. '
            "Teammates only see what you explicitly send. If another worker needs your findings, send them "
            "before finishing. Share concrete findings and evidence. Use your judgment to start your assigned work. "
            "If you need a teammate's work, ask via the available protocol. If responsible "
            "for a final output, write it, upload it and submit its exact ID. "
            "Use only your allowed actions. End with one JSON object, no markdown. Use kind=ready to finish "
            "your own work. Submit via submissions mapping output slot to an uploaded file ID. "
            "Files is a list of uploaded IDs. ready=true on a communication marks your own work complete.",
        }

    @property
    def running(self):
        with self.store.transaction() as db:
            return self._state(db).status == "running"

    async def step(self):
        async with self.lock:
            with self.store.transaction() as db:
                state = self._state(db)
                if state.status != "running":
                    return
                if state.inflight:
                    raise RuntimeError("已有成员在运行")
                if state.turns >= self.max_turns or (
                    self.max_team_tokens is not None
                    and budget.total(db) >= self.max_team_tokens
                ):
                    state.status = "budget_exhausted"
                    state.detail = (
                        f"已用完 {self.max_turns} 个工作回合"
                        if state.turns >= self.max_turns
                        else f"已用完 {self.max_team_tokens} 团队 tokens"
                    )
                    emit(db, "stopped", status=state.status, error=state.detail)
                    self.save(db, state)
                    return
                member = state.members[state.next]
                if self.agent_at_limit(db, member):
                    self.retire(db, state, member)
                    self.advance(db, state)
                    self.save(db, state)
                    return
                turn = uuid4().hex
                state.inflight = turn
                state.turns += 1
                mail = messages.inbox(db, member.id)
                prompt = self.public_input(state, member, mail)
                self.save(db, state)
                emit(
                    db,
                    "activated",
                    actor=member.id,
                    reason=state.reason,
                    turn_id=turn,
                    session_id=member.session_id,
                    messages=[m["id"] for m in mail],
                    input=prompt,
                )
            try:
                await self._run_turn(state, member, prompt, [m["id"] for m in mail])
            except InvalidAction as error:
                self.fail("invalid_action", error)
            except (HarnessError, budget.Exhausted) as error:
                with self.store.transaction() as db:
                    limit = db.execute(
                        "SELECT data FROM events WHERE kind='budget_exhausted' AND seq > "
                        "(SELECT MAX(seq) FROM events WHERE kind='activated') ORDER BY seq DESC LIMIT 1"
                    ).fetchone()
                    team_limit = (
                        self.max_team_tokens is not None
                        and budget.total(db) >= self.max_team_tokens
                    )
                    if self.agent_at_limit(db, member) and not team_limit:
                        self.retire(db, state, member)
                        self.advance(db, state)
                        self.save(db, state)
                        return
                self.fail(
                    "budget_exhausted" if team_limit or limit else "harness_error",
                    json.loads(limit[0])["error"] if limit else error,
                )
                return
            except Exception as error:  # noqa: BLE001 — 基础设施故障与模型回合错误单独归档
                self.fail("infra_error", error)
                return

    async def _run_turn(self, state, member, prompt, received):
        turn = state.inflight
        for attempt in (1, 2):
            result = await self.sandbox.turn(member, prompt, turn)
            try:
                self.commit(turn, member.id, result, received)
                return
            except InvalidAction as error:
                # 动作事务已回滚；原生会话、已上传文件和已用 tokens 均保留。
                member.session_id = result["session_id"]
                member.started = True
                with self.store.transaction() as db:
                    if not result.get("metered"):
                        budget.record(
                            db,
                            member.id,
                            member.category,
                            result.get("tokens", 0),
                            result.get("usd"),
                            cached_tokens=result.get("cached_tokens", 0),
                        )
                    exhausted = self.agent_at_limit(db, member) or (
                        self.max_team_tokens is not None
                        and budget.total(db) >= self.max_team_tokens
                    )
                    correction = None
                    if attempt == 1 and not exhausted:
                        correction = {
                            "error": str(error),
                            "actions": self.actions,
                            "schema": Action.model_json_schema(),
                            "instructions": "Your last action was rejected. Correct it and return one JSON object. "
                            "Use the body field specified for your action. Keep valid recipients and uploaded file IDs. "
                            "Do not repeat task work or uploads.",
                        }
                    emit(
                        db,
                        "action_rejected",
                        actor=member.id,
                        turn_id=turn,
                        session_id=member.session_id,
                        attempt=attempt,
                        result=result,
                        error=str(error),
                        correction_input=correction,
                    )
                    self.save(db, state)
                if exhausted:
                    raise budget.Exhausted("动作被拒绝时 token 预算已用尽") from error
                if correction is None:
                    raise
                prompt = correction

    def commit(self, turn, actor, result, received):
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM turns WHERE id=?", (turn,)).fetchone():
                return
            state = self._state(db)
            if (state.inflight, state.next) != (turn, actor):
                raise ValueError("回合与活动成员不匹配")
            member = state.members[actor]
            if not result.get("metered"):
                budget.record(
                    db,
                    actor,
                    member.category,
                    result.get("tokens", 0),
                    result.get("usd"),
                    cached_tokens=result.get("cached_tokens", 0),
                )
            db.execute("INSERT INTO turns VALUES(?,?,?)", (turn, actor, encode(result)))
            used = (
                budget.agent_used(db, actor)
                if self.max_agent_tokens is not None
                else None
            )
            if used is not None and used > self.max_agent_tokens:
                self.retire(db, state, member)
                self.advance(db, state)
                self.save(db, state)
                return
            member.session_id = result.get("session_id")
            if not member.session_id:
                raise HarnessError("缺少原生会话 ID")
            try:
                action = Action.model_validate(result["action"]).root
                member.started = True
                member.ready = action.ready or action.kind == "ready"
                if actor in state.initial:
                    state.initial.remove(actor)
                messages.consume(db, actor, received)
                for slot, key in action.submissions.items():
                    archive.submit(db, self.task, actor, slot, key, turn)
                state.next = None
                if action.kind == "ready":
                    if action.to or action.files:
                        raise ValueError("就绪动作不能指定收件人或发信附件")
                else:
                    self.handoff(db, state, member, action)
            except ValueError as error:
                raise InvalidAction(str(error)) from error
            if used is not None and used == self.max_agent_tokens:
                self.retire(db, state, member)
            self.advance(db, state)
            used = budget.total(db) if self.max_team_tokens is not None else 0
            if self.max_team_tokens is not None and (
                used > self.max_team_tokens
                or (state.status == "running" and used >= self.max_team_tokens)
            ):
                state.status = "budget_exhausted"
                state.detail = (
                    f"已使用 {used} tokens，团队上限为 {self.max_team_tokens}"
                )
            emit(
                db,
                "committed",
                turn_id=turn,
                actor=actor,
                session_id=member.session_id,
                action=action.model_dump(),
                next=state.next,
                reason=state.reason,
                status=state.status,
            )
            state.inflight = ""
            self.save(db, state)

    def advance(self, db, state):
        if state.next:
            return
        pending = messages.pending(db)
        if pending:
            state.next, state.reason = pending[0], "inbox"
        elif state.initial:
            state.next = self.choice(state, state.initial)
            state.reason = "initial_assignment"
        else:
            workers = state.members.values()
            missing = sorted(self.task.outputs.keys() - archive.latest(db).keys())
            unfinished = sorted(m.id for m in workers if not m.ready)
            state.status = "stalled" if missing or unfinished else "completed"
            if state.status == "stalled":
                state.detail = f"无人可唤醒。缺少交付项 {missing}，未就绪成员 {unfinished}"
                depleted = [m.id for m in workers if m.budget_exhausted]
                if depleted:
                    state.status = "budget_exhausted"
                    state.detail += f"，额度耗尽成员 {depleted}"
            state.reason = ""

    def choice(self, state, candidates):
        return random.Random(f"{self.seed}:{state.turns}").choice(sorted(candidates))

    def agent_at_limit(self, db, member):
        return (
            self.max_agent_tokens is not None
            and budget.agent_used(db, member.id) >= self.max_agent_tokens
        )

    def retire(self, db, state, member):
        member.budget_exhausted = True
        if state.next == member.id:
            state.next = None
        if member.id in state.initial:
            state.initial.remove(member.id)
        mail = messages.inbox(db, member.id)
        messages.consume(db, member.id, [m["id"] for m in mail])
        emit(
            db,
            "agent_budget_exhausted",
            actor=member.id,
            turn_id=state.inflight or None,
            used=budget.agent_used(db, member.id),
            limit=self.max_agent_tokens,
        )
        self.release_exhausted(db, state, member, mail)
        state.inflight = ""

    def deliver(
        self,
        db,
        state,
        sender,
        target,
        text,
        files=(),
        *,
        reason,
        display=None,
    ):
        if state.members[target].budget_exhausted:
            emit(
                db,
                "undeliverable",
                sender=sender,
                recipient=target,
                reason="agent_budget_exhausted",
            )
            return
        messages.send(
            db,
            sender,
            target,
            text,
            files,
            display=display,
            turn_id=state.inflight,
        )
        state.members[target].ready = False
        state.next, state.reason = target, reason

    def handoff(self, db, state, member, action):
        if action.to == member.id:
            raise ValueError("需要向其他成员交棒")
        target = state.members.get(action.to)
        if target is None:
            raise ValueError("目标工作成员不存在")
        if target.budget_exhausted:
            self.deliver(
                db,
                state,
                member.id,
                member.id,
                f"{target.id} 的预算已用尽，请选择其他成员。",
                display="team",
                reason="budget_return",
            )
            return
        self.deliver(
            db,
            state,
            member.id,
            target.id,
            action.message,
            action.files,
            reason="handoff",
        )

    def release_exhausted(self, db, state, member, mail):
        for sender in dict.fromkeys(m["from"] for m in mail):
            if sender in state.members and sender != member.id:
                self.deliver(
                    db,
                    state,
                    member.id,
                    sender,
                    f"{member.id} 的预算已用尽，未完成本次交棒。",
                    display="team",
                    reason="budget_return",
                )

    def result(self):
        with self.store.transaction() as db:
            state = self._state(db)
            return {
                "status": state.status,
                "error": state.detail,
                "turns": state.turns,
                "worker_count": len(state.members),
                "members": {k: asdict(m) for k, m in state.members.items()},
                "submissions": archive.latest(db),
                "usage": budget.summary(db),
            }
