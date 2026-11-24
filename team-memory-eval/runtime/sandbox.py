"""每轮启动一个无网络的成员容器，进程退出后才向调度器交回执行权。"""

import asyncio
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from runtime.envs.team import HarnessError
from scenarios import ROOT
from service.events import emit
from service.store import encode


class Sandbox:
    def __init__(self, root, config, service, timeout=None):
        self.root, self.config, self.service, self.timeout = (
            Path(root),
            config,
            service,
            timeout,
        )
        self.image_tag = "tme-" + config.harness + ":b1"
        image = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.image_tag],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
        if image.returncode:
            raise OSError(f"读取成员镜像 {self.image_tag} 失败: {image.stderr.strip()}")
        self.image_id = image.stdout.strip()
        with service.store.transaction() as db:
            emit(db, "sandbox_image", tag=self.image_tag, image_id=self.image_id)

    def path(self, member):
        return self.root / "members" / member.id

    def prepare(self, member):
        for name in ("work", "home"):
            (self.path(member) / name).mkdir(parents=True, exist_ok=True)

    async def turn(self, member, prompt, turn_id):
        name = "tme-turn-" + uuid4().hex
        path = self.path(member).resolve()
        cmd = [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "-i",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,nodev,size=512m",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "HOME=/state",
            "-e",
            "PYTHONPATH=/opt/tme",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "NO_PROXY=127.0.0.1,localhost",
            "-v",
            f"{path / 'work'}:/work",
            "-v",
            f"{path / 'home'}:/state",
            "-v",
            f"{ROOT / 'runtime/harnesses'}:/opt/tme/runtime/harnesses:ro",
            "-v",
            f"{ROOT / 'tools/__init__.py'}:/opt/tme/tools/__init__.py:ro",
            "-v",
            f"{ROOT / 'tools/client.py'}:/opt/tme/tools/client.py:ro",
            "-v",
            f"{self.service.socket}:/tme/service.sock:ro",
            self.image_id,
        ]
        with self.service.lease(member) as token:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            req = {
                "harness": self.config.harness,
                "model": self.config.model,
                "member_reasoning_effort": getattr(self.config, "member_reasoning_effort", None),
                "token": token,
                "member": asdict(member),
                "input": prompt,
            }
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(encode(req).encode()), timeout=self.timeout
                )
            except BaseException as error:
                cleanup = await asyncio.create_subprocess_exec(
                    "docker",
                    "rm",
                    "-f",
                    name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await cleanup.wait()
                await proc.wait()
                if isinstance(error, TimeoutError):
                    raise HarnessError(
                        f"成员 {member.id} 在 {self.timeout} 秒内未结束回合"
                    ) from error
                raise
        if proc.returncode:
            raise OSError(
                f"docker worker exit {proc.returncode}: {err.decode(errors='replace')[-1000:]}"
            )
        try:
            reply = json.loads(out.splitlines()[-1])
        except (ValueError, IndexError) as error:
            raise HarnessError(
                f"成员未返回结果: {err.decode(errors='replace')[-500:]}"
            ) from error
        if not reply["ok"]:
            raise HarnessError(reply["error"])
        result = reply["output"]
        text = (result.pop("text") or "").strip()
        if text.startswith("```") and text.endswith("```"):
            text = text.split("\n", 1)[-1][:-3].strip()
        try:
            action = json.loads(text)
        except ValueError:
            action = text
        return {**result, "action": action, "metered": True}
