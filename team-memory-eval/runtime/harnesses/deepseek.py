import json

from deepseek_harness import DeepSeekHarness


def turn(member, payload):
    with DeepSeekHarness(
        provider="deepseek-official",
        model=payload["model"],
        reasoning_effort=payload.get("member_reasoning_effort"),
        cwd="/work",
        dsh_home="/state/dsh",
        profile="sdk-minimal",
        base_url=payload["url"],
        api_key="tme",
        max_tokens=8192,
    ) as harness:
        session = (
            harness.resume_session(member["session_id"])
            if member["session_id"]
            else harness.start_session()
        )
        res = session.run(json.dumps(payload["input"], ensure_ascii=False))
        if res.finish_reason in {"error", "failed", "aborted"}:
            reasons = [
                e.get("data", {}).get("reason")
                for e in res.events
                if e.get("type") == "turn/end"
            ]
            raise RuntimeError(
                f"DeepSeek {res.finish_reason}: {res.final_response or reasons}"
            )
        return {
            "session_id": res.session_id,
            "text": res.final_response,
            "reason": res.finish_reason,
        }
