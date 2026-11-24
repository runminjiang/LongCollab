"use strict";
const $ = (id) => document.getElementById(id);
const labels = {
  reset: "载入任务",
  planner_called: "开始分工",
  planner_response: "规划器回复",
  planned: "团队分工已确定",
  activated: "成员接棒",
  sent: "消息投递",
  committed: "回合结束",
  environment_tool: "读取环境信息",
  uploaded: "上传附件",
  downloaded: "读取附件",
  submitted: "正式提交",
  model_request: "模型调用",
  model_error: "模型调用失败",
  action_rejected: "动作被拒绝",
  stopped: "运行停止",
  budget_exhausted: "预算耗尽",
  agent_budget_exhausted: "成员预算耗尽",
  budget_return: "成员额度耗尽，退回请求",
  undeliverable: "无法投递",
  run_finished: "运行归档",
  sandbox_image: "准备执行环境",
  judge_request: "评审模型调用",
  ready: "已就绪",
  planning: "规划中",
  running: "运行中",
  completed: "已完成",
  cancelled: "已取消",
  new: "准备中",
  stalled: "停滞",
  harness_error: "执行器错误",
  invalid_action: "动作无效",
  infra_error: "环境错误",
  planning_failed: "分工失败",
  planner_first: "Planner 指定首发",
  initial_assignment: "首次开工",
  handoff: "点对点交棒",
  inbox: "收件唤醒",
  point_to_point: "点对点",
  worker: "成员",
  scored: "已评分",
  not_run: "未评分",
  evaluation_error: "评分失败",
  assistant: "助手",
  user: "输入",
  system: "系统",
  tool: "工具",
};
const label = (value) => labels[value] || value || "—";
const fmt = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));
const clock = (time) =>
  time && Number.isFinite(Date.parse(time))
    ? new Date(time).toLocaleTimeString("zh-CN", { hour12: false })
    : "—";
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function button(text, cls, action) {
  const node = el("button", text, cls);
  node.onclick = action;
  return node;
}
function disclosure(title, body) {
  const details = el("details");
  details.append(el("summary", title), el("pre", fmt(body)));
  return details;
}
function empty(node, message) {
  node.append(el("p", message, "empty"));
}
function warning(message = "") {
  $("notice").hidden = !message;
  $("notice").textContent = message;
}
async function get(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error((await r.json()).error || "读取失败");
  return r.json();
}
let runs = [],
  source = null,
  runName = "",
  events = [],
  meta = {},
  member = "",
  pinned = false;
let tab = "input",
  selection = null,
  cursor = null,
  generation = 0,
  loading = false,
  archived = false;
let feedLimit = 100;
let history = { version: "", records: [] },
  historyKey = "",
  historyRequest = 0,
  imported = null,
  autoDemo = false;
const api = (route, params = {}) =>
  "/api/" + route + "?" + new URLSearchParams({ run: runName, ...params });
function library() {
  const node = $("library");
  node.replaceChildren();
  for (const [type, name, title] of [
    ...runs.map((n) => ["run", n, n]),
    ...(imported ? [["file", imported.name, imported.name]] : []),
    ["example", "", "示例轨迹"],
  ]) {
    const b = button(title, "lib", () => openRun(type, name));
    b.classList.toggle("active", type === source && name === runName);
    node.append(b);
  }
}
function decodeLog(text) {
  const rows = text
    .split(/\r?\n/)
    .filter((s) => s.trim())
    .map((line) => JSON.parse(line));
  if (
    !rows.length ||
    rows.some(
      (e, i) =>
        !e ||
        typeof e.kind !== "string" ||
        !Number.isInteger(e.seq) ||
        (i > 0 && e.seq <= rows[i - 1].seq),
    )
  ) {
    throw new Error("请选择框架导出的 events.jsonl，事件必须按序排列。");
  }
  return rows;
}
async function openRun(type, name, automatic = false) {
  const epoch = ++generation;
  source = type;
  runName = name;
  autoDemo = type === "example" && automatic;
  feedLimit = 100;
  events = [];
  meta = {};
  member = "";
  pinned = false;
  tab = "input";
  cursor = null;
  selection = null;
  archived = false;
  loading = false;
  historyKey = "";
  history = { version: "", records: [] };
  historyRequest++;
  $("own").checked = false;
  warning();
  library();
  render();
  try {
    if (type === "run") return await refresh(epoch);
    if (type === "file") events = imported.events;
    else {
      const r = await fetch("/example.jsonl");
      if (!r.ok) throw new Error("示例读取失败");
      const data = decodeLog(await r.text());
      if (epoch !== generation) return;
      events = data;
    }
    render();
  } catch (error) {
    if (epoch === generation) warning(error.message);
  }
}
async function refresh(epoch = generation) {
  if (source !== "run" || loading || archived) return;
  loading = true;
  try {
    const update = await get(api("trace", { after: events.at(-1)?.seq || 0 }));
    if (epoch !== generation) return;
    const changed =
      update.events.length ||
      JSON.stringify([update.members, update.result, update.scores]) !==
        JSON.stringify([meta.members, meta.result, meta.scores]);
    events = events.concat(update.events);
    meta = update;
    archived = Boolean(
      (update.result.status && update.scores.status) ||
      (!update.has_db && events.at(-1)?.kind === "run_finished"),
    );
    $("sync").textContent = archived ? "已归档" : "● 自动刷新";
    warning();
    if (changed) render();
  } catch (error) {
    if (epoch === generation) {
      warning(error.message);
      $("sync").textContent = "连接中断 · 正在重试";
    }
  } finally {
    if (epoch === generation) loading = false;
  }
}
function project(rows) {
  const people = new Map(),
    mail = new Map(),
    turns = [],
    related = new Map();
  let active = "",
    next = "",
    status = "new",
    task = "";
  function seat(id, info = {}) {
    if (!id) return;
    if (!people.has(id))
      people.set(id, {
        id,
        assignment: "",
        role: id,
        phase: "待开工",
        turns: 0,
      });
    const p = people.get(id);
    for (const key of ["profile", "assignment", "role", "category"])
      if (info[key]) p[key] = info[key];
    return p;
  }
  for (const e of rows) {
    let actor = e.actor;
    if (e.kind === "reset") task = e.task;
    if (e.kind === "planner_called") status = "planning";
    if (e.kind === "planned") {
      for (const p of e.plan.members) seat(p.id, p);
      status = "running";
      next = e.plan.first_member;
    }
    if (e.kind === "activated") {
      active = actor;
      next = "";
      status = "running";
      const p = seat(actor, {
        ...e.input,
        category: e.input?.role_type,
      });
      p.turns++;
      p.phase = "执行中";
      turns.push(e);
    }
    if (e.kind === "sent") {
      mail.set(e.message_id, e);
      seat(e.recipient).phase = "待接棒";
    }
    if (e.kind === "committed") {
      const p = seat(actor);
      if (p.phase !== "额度耗尽") {
        p.phase =
          e.action?.ready || e.action?.kind === "ready" ? "已就绪" : "等待唤醒";
      }
      active = "";
      next = e.next;
      status = e.status;
    }
    if (e.kind === "agent_budget_exhausted") {
      seat(actor).phase = "额度耗尽";
      if (active === actor) active = "";
      if (next === actor) next = "";
    }
    if (e.kind === "stopped" || e.kind === "run_finished") {
      if (active) {
        seat(active).phase = "已中止";
        actor = active;
      }
      active = "";
      next = "";
      status = e.status;
    }
    related.set(e.seq, [actor, e.sender, e.recipient].filter(Boolean));
  }
  // 回放到末尾时，用归档中的最终名单和状态补全界面。
  if (cursor === null && source === "run") {
    Object.values(meta.members || {}).forEach((p) => seat(p.id, p));
    if (meta.result?.status) status = meta.result.status;
  }
  if (next && status === "running") seat(next).phase = "待接棒";
  return { people, mail, turns, related, active, next, status, task };
}
function currentRows() {
  return cursor === null ? events : events.slice(0, cursor);
}
function color(id, view) {
  return "c" + (Array.from(view.people.keys()).indexOf(id) % 5);
}
function avatar(id, view) {
  const initials =
    id
      .replace(/^agent/i, "")
      .slice(0, 2)
      .toUpperCase() || "·";
  return el("span", initials, "av " + color(id, view));
}
function chooseMember(id) {
  member = id;
  pinned = true;
  selection = null;
  if (tab === "detail") tab = "input";
  render();
}
function eventTitle(e) {
  if (e.kind === "sent") return e.sender + " → " + e.recipient;
  if (e.kind === "activated") return e.actor + " 接棒";
  if (e.kind === "committed") return e.actor + " · " + label(e.action?.kind);
  if (e.kind === "environment_tool") return e.actor + " · " + e.op;
  return label(e.kind);
}
function messageBody(item) {
  return item?.message ?? item?.text;
}
function excerpt(e) {
  return (
    e.error ||
    messageBody(e) ||
    messageBody(e.action) ||
    (e.kind === "activated" ? label(e.reason) : "") ||
    (e.kind === "model_request"
      ? [e.actor, e.model, e.http_status].filter(Boolean).join(" · ")
      : "") ||
    [e.actor, e.name, e.slot, e.status && label(e.status), e.task]
      .filter(Boolean)
      .join(" · ") ||
    label(e.kind)
  );
}
function render() {
  const rows = currentRows(),
    view = project(rows),
    ids = [...view.people.keys()];
  if (!pinned || !ids.includes(member))
    member =
      view.active || view.next || view.turns.at(-1)?.actor || ids[0] || "";
  $("sync").textContent =
    source === "run"
      ? archived
        ? "已归档"
        : "● 自动刷新"
      : source === "file"
        ? "本地文件"
        : "脚本示例";
  const cfg = meta.config || {};
  $("runMeta").textContent = [
    view.task || cfg.task_id,
    cfg.harness,
    cfg.protocol && label(cfg.protocol),
    source === "example" ? "示例轨迹" : runName,
  ]
    .filter(Boolean)
    .join("  /  ");
  const isError = [
    "stalled",
    "invalid_action",
    "budget_exhausted",
    "infra_error",
    "planning_failed",
    "harness_error",
    "cancelled",
  ].includes(view.status);
  $("status").replaceChildren(
    el(
      "span",
      label(view.status),
      "badge " + (isError ? "error" : view.status === "running" ? "wait" : ""),
    ),
  );
  $("metrics").replaceChildren(
    ...[
      ["团队成员", ids.length, "♧"],
      ["执行回合", view.turns.length, "↻"],
      ["投递消息", view.mail.size, "⇄"],
      ["轨迹事件", rows.length.toLocaleString(), "≡"],
    ].map(([name, value, icon]) => {
      const n = el("div", null, "metric");
      n.append(el("label", name), el("b", value), el("span", icon, "meticon"));
      return n;
    }),
  );
  $("rosterCount").textContent = view.active
    ? "当前执行 · " + view.active
    : view.next && view.status === "running"
      ? "下一位 · " + view.next
      : "";
  const list = $("members");
  list.replaceChildren();
  for (const p of view.people.values()) {
    const b = button("", "member", () => chooseMember(p.id));
    b.classList.toggle("active", p.id === member);
    b.setAttribute("aria-pressed", p.id === member);
    const txt = el("span");
    txt.append(
      el("span", p.id, "memid"),
      el("small", p.phase),
    );
    b.append(avatar(p.id, view), txt);
    list.append(b);
  }
  if (!ids.length) empty(list, "等待团队分工");
  const feed = $("events"),
    top = feed.scrollTop;
  feed.replaceChildren();
  const matching = rows.filter(
    (e) => !$("own").checked || view.related.get(e.seq).includes(member),
  );
  for (const e of [...matching].reverse().slice(0, feedLimit)) {
    const b = button("", "event", () => {
      const person = e.actor || e.sender || e.recipient;
      if (view.people.has(person)) {
        member = person;
        pinned = true;
      }
      selection = e.seq;
      tab = "detail";
      render();
    });
    const txt = el("span");
    txt.append(el("strong", eventTitle(e)), el("small", excerpt(e)));
    b.append(el("i"), txt, el("time", clock(e.time)));
    b.classList.toggle("active", e.seq === selection);
    b.classList.toggle("transfer", e.kind === "sent");
    b.classList.toggle("failure", !!e.error);
    feed.append(b);
  }
  if (matching.length > feedLimit)
    feed.append(
      button("显示更早的事件", "older", () => {
        feedLimit += 100;
        render();
      }),
    );
  if (!matching.length) empty(feed, "此位置还没有对应事件");
  feed.scrollTop = top;
  $("inspectorId").textContent = member;
  const p = view.people.get(member),
    identity = $("identity");
  identity.replaceChildren();
  if (p) {
    const card = el("div", null, "identity"),
      txt = el("div");
    txt.append(
      el("strong", p.role || p.id),
      el("p", p.assignment || label(p.category)),
    );
    const latest = rows.findLast((e) =>
      view.related.get(e.seq).includes(member),
    );
    if (latest)
      txt.append(
        el(
          "small",
          "已唤醒 " +
            p.turns +
            " 次 · 最近一步 " +
            eventTitle(latest) +
            " · " +
            clock(latest.time),
          "summaryline",
        ),
      );
    card.append(avatar(member, view), txt);
    identity.append(card);
  }
  document.querySelectorAll("[data-tab]").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
    b.setAttribute("aria-pressed", b.dataset.tab === tab);
  });
  renderDetail(rows, view);
  const count = cursor ?? events.length;
  $("cursor").max = events.length;
  $("cursor").value = count;
  $("position").textContent = count + " / " + events.length;
  $("prev").disabled = count <= 0;
  $("next").disabled = count >= events.length;
  $("follow").classList.toggle("active", cursor === null);
  $("follow").textContent =
    source === "run" && !archived
      ? cursor === null
        ? "暂停跟随"
        : "跟随最新"
      : "跳到末尾";
}
function fileLinks(container, ids, rows) {
  for (const id of ids || []) {
    const name =
      rows.find((e) => e.kind === "uploaded" && e.file === id)?.name || id;
    if (source === "run" && meta.has_db) {
      const a = el("a", "↓ " + name, "filelink");
      a.href = api("file", { id });
      a.download = name;
      container.append(a);
    } else container.append(el("small", "附件 · " + name + " ", "filelink"));
  }
}
function renderDetail(rows, view) {
  const box = $("detail"),
    key = [generation, tab, member, selection].join("/");
  const same = box.dataset.key === key,
    top = same ? box.scrollTop : 0;
  const opened = new Set(
    same
      ? [...box.querySelectorAll("details[open]")].map((d) => d.dataset.key)
      : [],
  );
  box.dataset.key = key;
  box.replaceChildren();
  if (tab === "native") {
    if (source !== "run")
      empty(box, "原生会话保存在完整运行目录中。单个事件文件包含团队级轨迹。");
    else if (member) {
      if (historyKey !== generation + "/" + member) {
        historyKey = generation + "/" + member;
        history = { version: "", records: [] };
        fetchHistory();
      }
      const cutoff =
        cursor === null ? Infinity : Date.parse(rows.at(-1)?.time || "") || 0;
      const records = history.records.filter(
        (r) => cursor === null || Date.parse(r.time) <= cutoff,
      );
      if (!records.length)
        empty(
          box,
          history.error ||
            (history.version
              ? "此位置没有已保存的原生消息。"
              : "读取原生会话…"),
        );
      for (const r of [...records].reverse()) {
        const n = el("section");
        n.append(
          el("h3", label(r.role) + " · " + clock(r.time)),
          el("small", r.session, "summaryline"),
        );
        for (const b of r.content) {
          if (b.type === "text") n.append(disclosure("消息正文", b.text));
          else if (b.type === "tool")
            n.append(disclosure("调用 " + (b.name || "工具"), b.input));
          else if (b.type === "tool_result")
            n.append(
              disclosure(
                "工具返回 · " + (b.id || ""),
                b.content?.map((c) => c.text ?? fmt(c)).join("\n"),
              ),
            );
          else n.append(el("p", b.media_type || label(b.type)));
        }
        box.append(n);
      }
    } else empty(box, "请选择一名成员");
  } else if (tab === "detail") {
    const e = rows.find((e) => e.seq === selection) || rows.at(-1);
    if (!e) empty(box, "选择时间线上的事件查看详情");
    else {
      box.append(
        el("h3", eventTitle(e)),
        el("small", "#" + e.seq + " · " + (e.time || "")),
      );
      const body = messageBody(e) || e.error || messageBody(e.action);
      if (body) box.append(el("p", body, "entry"));
      fileLinks(
        box,
        e.files || e.action?.files || (e.file ? [e.file] : []),
        rows,
      );
      if (e.kind === "environment_tool")
        box.append(
          disclosure("调用参数", e.args),
          disclosure("返回给成员", e.result ?? "旧轨迹未记录工具返回值"),
        );
      if (e.kind === "run_finished") {
        if (meta.result?.error) box.append(el("p", meta.result.error, "entry"));
        if (meta.scores?.status)
          box.append(
            el("p", label(meta.scores.status)),
            disclosure("评分记录", meta.scores),
          );
      }
      box.append(disclosure("完整事件", e));
    }
  } else {
    const p = view.people.get(member);
    if (p?.profile) box.append(disclosure("专家角色档案", p.profile));
    // 先按实际唤醒归组。sent 表示已投递，只有 activated 中引用的消息才算这一轮收到。
    const turns = view.turns.filter((e) => e.actor === member);
    if (!turns.length) empty(box, "该成员尚无唤醒记录");
    const byTurn = new Map();
    let turn;
    for (const e of rows) {
      if (e.kind === "activated") {
        turn = e.actor === member ? e.turn_id : null;
        if (turn) byTurn.set(turn, []);
      } else if (
        turn &&
        e.actor === member &&
        [
          "environment_tool",
          "downloaded",
          "uploaded",
          "model_request",
          "committed",
          "action_rejected",
        ].includes(e.kind)
      )
        byTurn.get(turn).push(e);
      if (["committed", "stopped", "run_finished"].includes(e.kind))
        turn = null;
    }
    turns
      .map((e, i) => [e, i])
      .reverse()
      .forEach(([e, i]) => {
        const n = el("section");
        const title = el("div", "第 " + (i + 1) + " 次唤醒", "turntitle");
        title.append(el("span", clock(e.time) + " · " + label(e.reason)));
        n.append(title);
        const payload = e.input;
        const incoming =
          payload?.mail ??
          (e.messages || []).map(
            (id) =>
              view.mail.get(id) || { text: "消息 #" + id + " 的正文缺失" },
          );
        for (const m of incoming) {
          const notice = el("div", null, "entry");
          notice.append(
            el("strong", "来自 " + (m.from || m.sender || "队友")),
            el("p", messageBody(m)),
          );
          fileLinks(notice, m.files, rows);
          n.append(notice);
        }
        if (payload?.task) n.append(disclosure("任务与授权材料", payload.task));
        if (payload?.roster)
          n.append(disclosure("当时可见的团队名册", payload.roster));
        if (payload) n.append(disclosure("完整回合输入", payload));
        else
          n.append(
            el("small", "旧轨迹未保存完整回合输入；上方按收件编号还原消息。"),
          );
        for (const item of byTurn.get(e.turn_id) || []) {
          if (item.kind === "environment_tool")
            n.append(
              disclosure("环境工具 · " + item.op, {
                参数: item.args,
                返回: item.result ?? "此轨迹未记录返回值",
              }),
            );
          else if (item.kind === "downloaded" || item.kind === "uploaded")
            fileLinks(n, [item.file], rows);
          else if (item.kind === "committed")
            n.append(
              disclosure(
                "发出的动作 · " + label(item.action.kind),
                item.action,
              ),
            );
          else if (item.kind === "action_rejected") {
            n.append(el("p", item.error, "entry"));
            if (item.correction_input)
              n.append(disclosure("发回原会话的纠错提示", item.correction_input));
          }
        }
        box.append(n);
      });
  }
  box.querySelectorAll("details").forEach((d, i) => {
    // 新回合插入列表开头时仍能保留使用者展开的材料。
    const section = d.closest("section");
    d.dataset.key =
      (section?.firstElementChild?.textContent || "") +
      "/" +
      d.firstElementChild.textContent +
      "/" +
      (section ? "" : i);
    d.open = opened.has(d.dataset.key);
  });
  box.scrollTop = top;
}
async function fetchHistory() {
  if (tab !== "native" || !member || source !== "run") return;
  const request = ++historyRequest,
    key = historyKey;
  try {
    const data = await get(
      api("history", { member, version: history.version }),
    );
    if (request !== historyRequest || key !== historyKey || tab !== "native")
      return;
    if (data.records || history.error) {
      history = {
        version: data.version,
        records: data.records ?? history.records,
      };
      const rows = currentRows();
      renderDetail(rows, project(rows));
    }
  } catch (e) {
    if (request === historyRequest && key === historyKey && tab === "native") {
      history.error = e.message;
      const rows = currentRows();
      renderDetail(rows, project(rows));
    }
  }
}
function seek(n) {
  cursor = Math.max(0, Math.min(events.length, n));
  selection = null;
  render();
}
$("prev").onclick = () => seek((cursor ?? events.length) - 1);
$("next").onclick = () => seek((cursor ?? events.length) + 1);
$("cursor").oninput = () => seek(Number($("cursor").value));
$("follow").onclick = () => {
  cursor =
    source === "run" && !archived && cursor === null ? events.length : null;
  selection = null;
  render();
};
$("own").onchange = render;
document.querySelectorAll("[data-tab]").forEach(
  (b) =>
    (b.onclick = () => {
      tab = b.dataset.tab;
      render();
    }),
);
$("openImport").onclick = () => $("file").click();
$("file").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    imported = { name: f.name, events: decodeLog(await f.text()) };
    await openRun("file", f.name);
  } catch (error) {
    warning(error.message);
  } finally {
    e.target.value = "";
  }
};
async function poll() {
  try {
    const list = await get("/api/runs");
    if (JSON.stringify(list) !== JSON.stringify(runs)) {
      runs = list;
      library();
    }
    if (!source || (autoDemo && runs.length)) {
      if (runs.length) await openRun("run", runs[0]);
      else await openRun("example", "", true);
    } else await refresh();
    if (tab === "native") await fetchHistory();
  } catch (e) {
    warning(e.message);
    $("sync").textContent = "连接中断 · 正在重试";
  } finally {
    setTimeout(poll, 1200);
  }
}
poll();
