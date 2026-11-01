"""实验看门狗：逐秒观测主机与 bench slice，命中硬闸即中止。

硬闸只看能改变结论的条件：可用内存低于安全线、slice 达到 MemoryHigh 持续 5 秒、
OOM/OOM-kill 增长。保护服务的外部探针在 runner 里补充，本模块不启动任何业务。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path


def read_meminfo() -> dict[str, int]:
    data: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        data[key] = int(rest.split()[0]) if rest.split() else 0
    return data


def read_vmstat() -> dict[str, int]:
    return {
        key: int(value)
        for key, value in (
            line.split() for line in Path("/proc/vmstat").read_text(encoding="utf-8").splitlines()
        )
    }


def read_pressure(kind: str) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for line in Path(f"/proc/pressure/{kind}").read_text(encoding="utf-8").splitlines():
        name = line.split()[0]
        for item in line.split()[1:]:
            key, _, value = item.partition("=")
            parsed[f"{name}_{key}"] = float(value)
    return parsed


@dataclass
class Watchdog:
    cgroup: Path | None
    mem_floor_mib: float = 2048.0
    interval: float = 1.0
    samples: list[dict[str, float]] = field(default_factory=list)
    aborted: bool = False
    reasons: list[str] = field(default_factory=list)
    peak_memory_current_mib: float = 0.0
    peak_memory_high_seconds: int = 0

    def _cgroup_file(self, name: str) -> str | None:
        if self.cgroup is None:
            return None
        path = self.cgroup / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    def _cgroup_value(self, name: str, key: str) -> float | None:
        text = self._cgroup_file(name)
        if text is None:
            return None
        for line in text.splitlines():
            if line.startswith(key + " "):
                return float(line.split()[1])
        return None

    def _cgroup_single(self, name: str) -> float | None:
        text = self._cgroup_file(name)
        if text is None or not text.split():
            return None
        return float(text.split()[0])

    def sample(self) -> dict[str, float]:
        memory = read_meminfo()
        vm = read_vmstat()
        psi = read_pressure("memory")
        current = self._cgroup_single("memory.current") or 0.0
        pressure_high = self._cgroup_value("memory.events", "high") or 0.0
        oom = self._cgroup_value("memory.events", "oom") or 0.0
        oom_kill = self._cgroup_value("memory.events", "oom_kill") or 0.0
        entry = {
            "t": time.monotonic(),
            "mem_available_mib": memory["MemAvailable"] / 1024.0,
            "swap_in_pages": float(vm["pswpin"]),
            "swap_out_pages": float(vm["pswpout"]),
            "memory_psi_full_avg60": psi.get("full_avg60", 0.0),
            "cgroup_memory_current_mib": current / 1024 / 1024,
            "cgroup_high_events": pressure_high,
            "cgroup_oom_events": oom,
            "cgroup_oom_kill_events": oom_kill,
        }
        self.samples.append(entry)
        self.peak_memory_current_mib = max(self.peak_memory_current_mib, entry["cgroup_memory_current_mib"])
        return entry

    def evaluate(self) -> None:
        if not self.samples:
            return
        latest = self.samples[-1]
        if latest["mem_available_mib"] < self.mem_floor_mib:
            self._abort("MEM_AVAILABLE_BELOW_FLOOR")
        if latest["cgroup_oom_events"] > 0 or latest["cgroup_oom_kill_events"] > 0:
            self._abort("CGROUP_OOM")
        if len(self.samples) >= 5:
            recent = self.samples[-5:]
            if all(item["cgroup_memory_current_mib"] >= 0 and item["cgroup_high_events"] > 0
                   for item in recent):
                self._abort("MEMORY_HIGH_SUSTAINED")
                self.peak_memory_high_seconds += 5

    def _abort(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.aborted = True

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.sample()
            self.evaluate()
            if self.aborted:
                stop.set()
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except TimeoutError:
                continue

    def summary(self) -> dict[str, object]:
        keys = ("mem_available_mib", "memory_psi_full_avg60", "cgroup_memory_current_mib")
        return {
            "samples": len(self.samples),
            "aborted": self.aborted,
            "reasons": self.reasons,
            "peak_cgroup_memory_mib": round(self.peak_memory_current_mib, 2),
            "min_mem_available_mib": round(
                min((item["mem_available_mib"] for item in self.samples), default=0.0), 2
            ),
            "max_memory_psi_full_avg60": round(
                max((item["memory_psi_full_avg60"] for item in self.samples), default=0.0), 3
            ),
            "max_cgroup_memory_mib": round(
                max((item["cgroup_memory_current_mib"] for item in self.samples), default=0.0), 2
            ),
            "keys": list(keys),
        }


