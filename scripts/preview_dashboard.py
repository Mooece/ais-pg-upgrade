"""Run the real dashboard locally with isolated, clearly labelled sample data.

Usage: uv run python scripts/preview_dashboard.py --port 8766
No Google credentials are loaded and no model requests are sent.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="本地面板演示（独立样例数据）")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--empty", action="store_true", help="预览无调用记录的界面")
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project / "src"))
    with tempfile.TemporaryDirectory(prefix="aistudio-dashboard-preview-") as directory:
        data_dir = Path(directory)
        config_path = data_dir / "config.yaml"
        config_path.write_text("api_keys: []\n", encoding="utf-8")
        for key, value in {
            "AISTUDIO_DATA_DIR": directory,
            "AISTUDIO_ACCOUNTS_DIR": str(data_dir / "accounts"),
            "AISTUDIO_STATS_FILE": str(data_dir / "stats.json"),
            "AISTUDIO_ROTATOR_STATE_FILE": str(data_dir / "rotator_state.json"),
            "AISTUDIO_CONFIG_FILE": str(config_path),
            "AISTUDIO_ANALYTICS_DEMO": "1",
            "AISTUDIO_WEB_PASSWORD": "",
            "AISTUDIO_ADMIN_PASSWORD": "",
            "AISTUDIO_AUTH_FILE": str(data_dir / "no-google-credentials.json"),
            "AISTUDIO_TIMEZONE": "Asia/Shanghai",
            "AISTUDIO_DUMP_REQUESTS": "0",
            "AISTUDIO_DUMP_FILE": "0",
            "DEBUG": "0",
            "AISTUDIO_DEBUG": "0",
        }.items():
            os.environ[key] = value

        import uvicorn

        from aistudio_api.api.analytics import (
            RequestRecord,
            RequestStatus,
            get_analytics_store,
        )
        from aistudio_api.api.app import app
        from aistudio_api.api.state import runtime_state

        if not args.empty:
            rng = random.Random(20261120)
            store = get_analytics_store()
            models = ["gemini-4-argon", "gemini-3.8-flash", "gemini-3.1-pro-preview"]
            statuses: list[RequestStatus] = [
                "success",
                "error",
                "rate_limited",
                "cancelled",
            ]
            now = datetime.now(UTC)
            for day in range(35, -1, -1):
                day_start = (now - timedelta(days=day)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                for index in range(rng.randint(8, 24)):
                    started = day_start + timedelta(seconds=rng.randint(0, 86399))
                    if started >= now - timedelta(minutes=15):
                        continue
                    model = rng.choices(models, weights=[6, 3, 1])[0]
                    prompt = rng.randint(8500, 65000)
                    completion = rng.randint(800, 4600)
                    cached = int(prompt * rng.uniform(0.3, 0.85))
                    reasoning = rng.randint(100, 600)
                    status = rng.choices(
                        statuses,
                        weights=[92, 3, 3, 2],
                    )[0]
                    success = status == "success"
                    stream = index % 4 != 0
                    elapsed = (
                        rng.uniform(38000, 148000)
                        if success
                        else rng.uniform(700, 11000)
                    )
                    record = RequestRecord(
                        started_at=started.isoformat(),
                        model=model,
                        endpoint="/v1/chat/completions"
                        if index % 5
                        else "/v1beta/models/*:generateContent",
                        stream=stream,
                        status=status,
                        status_code={
                            "success": 200,
                            "error": 400,
                            "rate_limited": 429,
                            "cancelled": 200,
                        }[status],
                        duration_ms=round(elapsed, 2),
                        ttft_ms=round(rng.uniform(600, 9500), 2)
                        if success and stream
                        else None,
                        prompt_tokens=prompt if success else 0,
                        completion_tokens=completion if success else 0,
                        cached_tokens=cached if success else 0,
                        reasoning_tokens=reasoning if success else 0,
                        total_tokens=prompt + completion if success else 0,
                        usage_known=success,
                        finish_reason="STOP" if success else None,
                    )
                    store.complete(record)
                    usage = {
                        "prompt_tokens": record.prompt_tokens,
                        "completion_tokens": record.completion_tokens,
                        "total_tokens": record.total_tokens,
                        "cached_tokens": record.cached_tokens,
                        "completion_tokens_details": {
                            "reasoning_tokens": record.reasoning_tokens
                        },
                    }
                    runtime_state.record(
                        model,
                        "success"
                        if success
                        else "rate_limited"
                        if status == "rate_limited"
                        else "errors",
                        usage=usage if success else None,
                        finish_reason=record.finish_reason,
                    )
            runtime_state.save_stats()

        print(f"本地演示面板：http://127.0.0.1:{args.port}")
        print("样例数据与真实账号、统计完全隔离；关闭预览后自动清理。")
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
