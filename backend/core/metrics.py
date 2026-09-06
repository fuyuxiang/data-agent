from __future__ import annotations

import threading
from collections import Counter


BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class RequestMetrics:
    """Small dependency-free Prometheus registry for the single web process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: Counter[tuple[str, str, int]] = Counter()
        self._duration_count: Counter[tuple[str, str]] = Counter()
        self._duration_sum: Counter[tuple[str, str]] = Counter()
        self._duration_buckets: Counter[tuple[str, str, float]] = Counter()

    def observe(self, method: str, route: str, status: int, duration_seconds: float) -> None:
        key = (method, route)
        with self._lock:
            self._requests[(method, route, status)] += 1
            self._duration_count[key] += 1
            self._duration_sum[key] += duration_seconds
            for bucket in BUCKETS:
                if duration_seconds <= bucket:
                    self._duration_buckets[(method, route, bucket)] += 1

    @staticmethod
    def _labels(**values: object) -> str:
        escaped = {
            key: str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            for key, value in values.items()
        }
        return ",".join(f'{key}="{value}"' for key, value in escaped.items())

    def render(self, *, jobs: list[dict], agent_runs: list[dict]) -> str:
        with self._lock:
            requests = dict(self._requests)
            duration_count = dict(self._duration_count)
            duration_sum = dict(self._duration_sum)
            duration_buckets = dict(self._duration_buckets)
        lines = [
            "# HELP meridian_http_requests_total HTTP requests completed.",
            "# TYPE meridian_http_requests_total counter",
        ]
        for (method, route, status), value in sorted(requests.items()):
            labels = self._labels(method=method, route=route, status=status)
            lines.append(f"meridian_http_requests_total{{{labels}}} {value}")
        lines.extend([
            "# HELP meridian_http_request_duration_seconds HTTP request latency.",
            "# TYPE meridian_http_request_duration_seconds histogram",
        ])
        for method, route in sorted(duration_count):
            cumulative = 0
            for bucket in BUCKETS:
                cumulative = duration_buckets.get((method, route, bucket), cumulative)
                labels = self._labels(method=method, route=route, le=bucket)
                lines.append(f"meridian_http_request_duration_seconds_bucket{{{labels}}} {cumulative}")
            labels = self._labels(method=method, route=route, le="+Inf")
            lines.append(f"meridian_http_request_duration_seconds_bucket{{{labels}}} {duration_count[(method, route)]}")
            base = self._labels(method=method, route=route)
            lines.append(f"meridian_http_request_duration_seconds_sum{{{base}}} {duration_sum[(method, route)]:.9f}")
            lines.append(f"meridian_http_request_duration_seconds_count{{{base}}} {duration_count[(method, route)]}")
        lines.extend([
            "# HELP meridian_jobs Jobs by current status.",
            "# TYPE meridian_jobs gauge",
        ])
        for status, count in sorted(Counter(str(item.get("status") or "unknown") for item in jobs).items()):
            lines.append(f"meridian_jobs{{{self._labels(status=status)}}} {count}")
        lines.extend([
            "# HELP meridian_agent_runs Agent runs by execution and quality status.",
            "# TYPE meridian_agent_runs gauge",
        ])
        counts = Counter(
            (str(item.get("execution_status") or "unknown"), str(item.get("quality_status") or "unknown"))
            for item in agent_runs
        )
        for (status, quality), count in sorted(counts.items()):
            lines.append(
                f"meridian_agent_runs{{{self._labels(status=status, quality_status=quality)}}} {count}",
            )
        return "\n".join(lines) + "\n"
