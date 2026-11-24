"""上游评价器在独立宿主进程执行，评委的成功调用与用量单独留证。"""

import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scenarios import ROOT, RUNTIME_CACHE, official_package


def official(request):
    bench, model = request["benchmark"], request["model"]
    official_package(bench)
    if bench == "marble":
        from marble.evaluator.evaluator import Evaluator

        evaluator = Evaluator({"evaluate_llm": {"model": "openai/" + model}})
        evaluator.evaluate_task_research(request["goal"], request["files"]["idea"])
        values = evaluator.metrics["task_evaluation"]
        if not values:
            raise ValueError("MARBLE 评分器未返回有效分数")
        return values
    if bench == "drbench":
        from drbench.score_report import score_report

        return score_report(
            predicted_report_dict=json.loads(request["files"]["report.json"]),
            **request["reference"],
            metrics=["insights_recall"],
            model="openrouter/" + model,
            include_per_insight_scores=False,
            upload_scores=False,
        )
    raise ValueError("未知基准")


def score(task, artifacts, model, gateway):
    token = secrets.token_urlsafe(32)
    errors = []
    successes = []

    class JudgeAPI(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                if self.path != "/v1/chat/completions" or not hmac.compare_digest(
                    self.headers.get("authorization", ""), "Bearer " + token
                ):
                    raise PermissionError("评委请求无效")
                if errors:
                    raise RuntimeError(errors[0])
                request = json.loads(
                    self.rfile.read(int(self.headers["content-length"]))
                )
                answer = gateway.judge_chat(request, model)
                successes.append(1)
                body, status = json.dumps(answer).encode(), 200
            except Exception as error:  # noqa: BLE001 — 评委错误通过 HTTP 返回并保留原文
                if not errors:
                    errors.append(f"{type(error).__name__}: {error}")
                body, status = (
                    json.dumps({"error": {"message": errors[0]}}).encode(),
                    400,
                )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), JudgeAPI) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/v1"
        env = {
            **os.environ,
            "OPENAI_BASE_URL": url,
            "OPENAI_API_BASE": url,
            "OPENAI_API_KEY": token,
            "OPENROUTER_API_URL": url,
            "OPENROUTER_API_KEY": token,
            "NO_PROXY": "127.0.0.1,localhost",
            "CUDA_VISIBLE_DEVICES": "",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HOME": str(RUNTIME_CACHE / "hf"),
            "TORCH_HOME": str(RUNTIME_CACHE / "torch"),
            "MPLCONFIGDIR": str(RUNTIME_CACHE / "mpl"),
            "XDG_CACHE_HOME": str(RUNTIME_CACHE),
        }
        payload = {
            "benchmark": task.benchmark,
            "goal": task.goal,
            "model": model,
            "reference": task.reference,
            "files": {k: v.decode() for k, v in artifacts.items()},
        }
        try:
            cmd = [sys.executable, "-m", "eval.score"]
            done = subprocess.run(
                cmd,
                check=False,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=env,
                cwd=ROOT,
                timeout=900,
            )
        finally:
            server.shutdown()
            thread.join()
    if errors:
        raise RuntimeError(errors[0])
    if done.returncode:
        raise RuntimeError(done.stderr.strip()[-1600:] or done.stdout.strip()[-1000:])
    if not successes:
        raise RuntimeError("官方评分器未产生成功的评委请求")
    return json.loads(done.stdout.splitlines()[-1])


def research_answers(paths):
    """读取独立运行留下的 Research JSONL，保留文件名和行号供核对。"""
    rows = []
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                location = f"{path}:{line_number}"
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"{location} JSON 无效：{error.msg}") from error
                if (
                    not isinstance(row, dict)
                    or row.get("scenario", "research") != "research"
                ):
                    raise ValueError(f"{location} 需要 Research JSON 记录")
                task_id = row.get("task_id")
                if type(task_id) is not int or task_id < 1:
                    raise ValueError(f"{location} task_id 需要为正整数")
                answer = row.get("answer", row.get("response"))
                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError(f"{location} 需要非空的 answer 或 response 文本")
                rows.append((str(path), line_number, f"research/{task_id}", answer))
    if not rows:
        raise ValueError("输入没有 Research 记录")
    return rows


def score_research_answers(paths, config):
    from tempfile import TemporaryDirectory

    from scenarios import SOURCES, load
    from service.gateway import Gateway, endpoint_url, is_validation_endpoint
    from service.store import Store

    rows = research_answers(paths)
    endpoint = endpoint_url(
        config.judge_provider, config.judge_base_url, judge=True, chat=True
    )
    model = config.judge_model
    results = []
    with TemporaryDirectory(prefix="tme-research-score-", dir="/tmp") as directory:
        store = Store(directory)
        try:
            gateway = Gateway(config, store)
            for source, line, task_id, answer in rows:
                result = {"source": source, "line": line, "task_id": task_id}
                results.append(result)
                try:
                    task = load("marble", task_id)
                    metrics = score(task, {"idea": answer.encode("utf-8")}, model, gateway)
                    ratings = [
                        metrics.get(key) for key in ("innovation", "safety", "feasibility")
                    ]
                    if any(
                        type(value) is not int or not 1 <= value <= 5 for value in ratings
                    ):
                        raise ValueError(
                            f"MARBLE 评分器未返回三项有效的 1–5 分：{metrics!r}"
                        )
                    ts = sum(ratings) / len(ratings)
                    result.update(status="scored", metrics=metrics, ts=ts, ts_100=ts * 20)
                except Exception as error:  # noqa: BLE001 — 保留每条记录的错误并继续处理后续题目
                    result.update(
                        status="evaluation_error",
                        error=f"{type(error).__name__}: {error}",
                    )
        finally:
            store.close()
    return {
        "benchmark": "marble",
        "revision": SOURCES["marble"][1],
        "judge_model": model,
        "purpose": (
            "framework_validation" if is_validation_endpoint(endpoint) else "evaluation"
        ),
        "cs": None,
        "cs_status": "missing_coordination_trace",
        "results": results,
    }


def main():
    if len(sys.argv) == 1:
        print(json.dumps(official(json.load(sys.stdin)), ensure_ascii=False))
        return 0

    import argparse
    from pathlib import Path

    import yaml

    from run import Config
    from service.store import write_json

    parser = argparse.ArgumentParser(
        description="给独立运行的 MARBLE Research JSONL 计算 TS"
    )
    parser.add_argument(
        "--config", required=True, type=Path, help="含评委设置的 TME 实验配置"
    )
    parser.add_argument(
        "--judge-provider",
        choices=("vllm", "openai", "anthropic"),
        help="覆盖配置中的评委协议",
    )
    parser.add_argument("--judge-base-url", help="覆盖评委 API 地址，如 https://host/v1")
    parser.add_argument("--judge-model", help="覆盖评委模型名")
    parser.add_argument(
        "--judge-api-key-env", help="保存评委 API key 的环境变量名，如 MARBLE_JUDGE_API_KEY"
    )
    parser.add_argument("--output", type=Path, help="评分 JSON 路径，缺省时写到标准输出")
    parser.add_argument("inputs", nargs="+", type=Path, help="Research 答案 JSONL")
    args = parser.parse_args()
    if args.output and args.output.resolve() in {
        path.resolve() for path in [args.config, *args.inputs]
    }:
        parser.error("输出路径不能覆盖配置或输入文件")
    try:
        data = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        judge_overrides = {
            key: value
            for key, value in vars(args).items()
            if key.startswith("judge_") and value is not None
        }
        if isinstance(data, dict):
            if any(
                key in judge_overrides and judge_overrides[key] != data.get(key)
                for key in ("judge_provider", "judge_base_url")
            ):
                # 换评委服务时，旧 YAML 中的凭据属于原服务。
                data.pop("judge_api_key", None)
                data.pop("judge_api_key_env", None)
            data.update(judge_overrides)
        # 待评分题目由输入文件逐行指定，只需取批次共用的评委设置。
        config = Config.for_tasks(data)[0]
        report = score_research_answers(args.inputs, config)
        if args.output:
            write_json(args.output, report)
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    return int(any(row["status"] != "scored" for row in report["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
