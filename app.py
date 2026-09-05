#!/usr/bin/env python3
"""Meridian Analytics Workbench entry point."""

from backend import create_app

app = create_app()

if __name__ == "__main__":
    import os

    host = os.getenv("MERIDIAN_HOST", "127.0.0.1")
    port = int(os.getenv("MERIDIAN_PORT", "5001"))
    debug = os.getenv("MERIDIAN_DEBUG", "0") == "1"
    if debug and app.config["SETTINGS"].environment == "production":
        raise RuntimeError("生产环境禁止启用 MERIDIAN_DEBUG")
    if debug:
        app.run(host=host, port=port, debug=True, threaded=True)
    else:
        try:
            from waitress import serve
        except ImportError as exc:
            if app.config["SETTINGS"].environment == "production":
                raise RuntimeError("生产环境必须安装锁定的 Waitress 依赖") from exc
            app.run(host=host, port=port, debug=False, threaded=True)
        else:
            serve(app, host=host, port=port, threads=12, channel_timeout=300)
