"""Performance profiling and metrics collection utilities."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional in minimal environments
    psutil = None


@dataclass
class PhaseMetrics:
    """Metrics for a single phase of execution."""

    duration_sec: float
    cpu_percent_start: float
    cpu_percent_end: float
    memory_mb_start: float
    memory_mb_end: float
    memory_mb_delta: float

    def to_dict(self) -> dict:
        return {
            "duration_sec": round(self.duration_sec, 4),
            "cpu_start": round(self.cpu_percent_start, 1),
            "cpu_end": round(self.cpu_percent_end, 1),
            "mem_start_mb": round(self.memory_mb_start, 1),
            "mem_end_mb": round(self.memory_mb_end, 1),
            "mem_delta_mb": round(self.memory_mb_delta, 1),
        }


@dataclass
class WorkerMetrics:
    """Complete metrics for one variant evaluation."""

    worker_id: int
    variant_idx: int
    phases: dict[str, PhaseMetrics] = field(default_factory=dict)
    total_duration_sec: float = 0.0
    process_id: int = 0
    cpu_count: int = 0

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "variant_idx": self.variant_idx,
            "process_id": self.process_id,
            "cpu_count": self.cpu_count,
            "total_duration_sec": round(self.total_duration_sec, 4),
            "phases": {name: metrics.to_dict() for name, metrics in self.phases.items()},
        }


class PerformanceProfiler:
    """Context manager for profiling a single phase."""

    def __init__(self, phase_name: str, metrics_collector: MetricsCollector):
        self.phase_name = phase_name
        self.collector = metrics_collector
        self.start_time = 0.0
        self.start_cpu = 0.0
        self.start_mem = 0.0
        self.process = psutil.Process() if psutil is not None else None

    def __enter__(self):
        if self.process is not None:
            self.process.cpu_percent(interval=None)
            time.sleep(0.001)

        self.start_time = time.perf_counter()
        if self.process is not None:
            self.start_cpu = self.process.cpu_percent(interval=None)
            self.start_mem = self.process.memory_info().rss / (1024 * 1024)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        end_time = time.perf_counter()
        if self.process is not None:
            end_cpu = self.process.cpu_percent(interval=None)
            end_mem = self.process.memory_info().rss / (1024 * 1024)
        else:
            end_cpu = 0.0
            end_mem = self.start_mem

        metrics = PhaseMetrics(
            duration_sec=end_time - self.start_time,
            cpu_percent_start=self.start_cpu,
            cpu_percent_end=end_cpu,
            memory_mb_start=self.start_mem,
            memory_mb_end=end_mem,
            memory_mb_delta=end_mem - self.start_mem,
        )

        self.collector.add_phase_metrics(self.phase_name, metrics)
        return False


class MetricsCollector:
    """Collect metrics for a single worker."""

    def __init__(self, worker_id: int, variant_idx: int):
        self.metrics = WorkerMetrics(
            worker_id=worker_id,
            variant_idx=variant_idx,
            process_id=os.getpid(),
            cpu_count=os.cpu_count() or 1,
        )
        self.start_time = time.perf_counter()

    def add_phase_metrics(self, phase_name: str, metrics: PhaseMetrics):
        self.metrics.phases[phase_name] = metrics

    def finalize(self) -> WorkerMetrics:
        self.metrics.total_duration_sec = time.perf_counter() - self.start_time
        return self.metrics

    def profile_phase(self, phase_name: str) -> PerformanceProfiler:
        return PerformanceProfiler(phase_name, self)


class PerformanceReport:
    """Aggregates and reports performance metrics across workers."""

    def __init__(self, all_metrics: list[WorkerMetrics]):
        self.all_metrics = all_metrics
        self.num_workers = len(all_metrics)

    def print_summary(self):
        if not self.all_metrics:
            print("No metrics collected.")
            return

        print("\n" + "=" * 80)
        print("PERFORMANCE PROFILING REPORT")
        print("=" * 80)

        total_time = sum(m.total_duration_sec for m in self.all_metrics)
        avg_time = total_time / len(self.all_metrics)
        min_time = min(m.total_duration_sec for m in self.all_metrics)
        max_time = max(m.total_duration_sec for m in self.all_metrics)

        print("\nOverall Summary:")
        print(f"  Workers evaluated: {len(self.all_metrics)}")
        print(f"  Total time (all workers): {total_time:.2f}s")
        print(f"  Average time per worker: {avg_time:.2f}s")
        print(f"  Min/Max worker time: {min_time:.2f}s / {max_time:.2f}s")
        print(f"  CPU cores available: {self.all_metrics[0].cpu_count}")

        print("\nPhase Breakdown (averaged across workers):")
        self._print_phase_summary()

        print("\nResource Usage:")
        self._print_resource_summary()

        print("\nPer-Worker Details:")
        self._print_worker_details()

        print("=" * 80 + "\n")

    def _print_phase_summary(self):
        all_phases: set[str] = set()
        for m in self.all_metrics:
            all_phases.update(m.phases.keys())

        if not all_phases:
            print("  No phase data available.")
            return

        phase_stats: dict[str, dict[str, float]] = {}
        for phase in sorted(all_phases):
            durations = [
                m.phases[phase].duration_sec for m in self.all_metrics if phase in m.phases
            ]
            mem_deltas = [
                m.phases[phase].memory_mb_delta for m in self.all_metrics if phase in m.phases
            ]

            if durations:
                phase_stats[phase] = {
                    "avg_time": sum(durations) / len(durations),
                    "min_time": min(durations),
                    "max_time": max(durations),
                    "avg_mem_delta": sum(mem_deltas) / len(mem_deltas),
                }

        for phase in sorted(phase_stats, key=lambda p: phase_stats[p]["avg_time"], reverse=True):
            stats = phase_stats[phase]
            print(
                f"  {phase:25} {stats['avg_time']:7.3f}s  "
                f"(min: {stats['min_time']:.3f}s, max: {stats['max_time']:.3f}s)  "
                f"[mem Δ: {stats['avg_mem_delta']:+.1f} MB]"
            )

    def _print_resource_summary(self):
        total_mem_deltas = [
            sum(p.memory_mb_delta for p in m.phases.values()) for m in self.all_metrics
        ]
        if total_mem_deltas:
            avg_mem = sum(total_mem_deltas) / len(total_mem_deltas)
            max_mem = max(total_mem_deltas)
            print(f"  Average memory delta per worker: {avg_mem:+.1f} MB")
            print(f"  Max memory delta (single worker): {max_mem:+.1f} MB")

        peak_mems = [
            max((p.memory_mb_end for p in m.phases.values()), default=0) for m in self.all_metrics
        ]
        if peak_mems:
            print(f"  Peak memory usage (max across workers): {max(peak_mems):.1f} MB")

    def _print_worker_details(self):
        for m in sorted(self.all_metrics, key=lambda x: x.worker_id):
            print(f"\n  Worker {m.worker_id} (Variant {m.variant_idx}, PID {m.process_id}):")
            print(f"    Total time: {m.total_duration_sec:.3f}s")

            if m.phases:
                print("    Phases:")
                for phase, metrics in sorted(m.phases.items()):
                    print(
                        f"      {phase:20} {metrics.duration_sec:7.3f}s  "
                        f"[mem: {metrics.memory_mb_start:.0f} → {metrics.memory_mb_end:.0f} MB, "
                        f"Δ {metrics.memory_mb_delta:+.1f} MB]"
                    )

    def export_json(self, filename: str):
        data = {
            "num_workers": self.num_workers,
            "workers": [m.to_dict() for m in self.all_metrics],
        }
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"Exported metrics to {filename}")


__all__ = [
    "PhaseMetrics",
    "WorkerMetrics",
    "PerformanceProfiler",
    "MetricsCollector",
    "PerformanceReport",
]
