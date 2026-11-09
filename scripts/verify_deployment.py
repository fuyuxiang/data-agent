#!/usr/bin/env python3
"""Log in through the public HTTPS endpoint and verify workspace initialization."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, OpenerDirector, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(opener: OpenerDirector, request: Request) -> dict:
    path = urlsplit(request.full_url).path
    try:
        with opener.open(request, timeout=30) as response:
            request_id = response.headers.get("X-Request-Id", "不可用")
            if response.status != 200:
                raise RuntimeError(f"{path} 返回 HTTP {response.status}（请求 ID {request_id}）")
            try:
                payload = json.load(response)
            except ValueError:
                raise RuntimeError(f"{path} 返回无效 JSON（请求 ID {request_id}）") from None
    except HTTPError as error:
        with error:
            request_id = error.headers.get("X-Request-Id", "不可用")
            raise RuntimeError(f"{path} 返回 HTTP {error.code}（请求 ID {request_id}）") from None
    except OSError:
        raise RuntimeError(f"{path} 连接失败，请检查 HTTPS 和网络连通性") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(f"{path} 响应未确认成功（请求 ID {request_id}）")
    return payload


def verify_deployment(base_url: str, username: str, password: str, workspace_id: str | None = None) -> str:
    url = urlsplit(base_url)
    if (
        url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None
        or url.path not in {"", "/"} or url.query or url.fragment
    ):
        raise ValueError("请输入站点的 HTTPS 根地址")
    origin = f"https://{url.netloc}"
    cookies = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies), NoRedirects())
    login = request_json(opener, Request(  # noqa: S310 - origin is restricted to HTTPS
        f"{origin}/api/auth/login",
        data=json.dumps({"username": username, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "Origin": origin},
    ))
    if not cookies:
        raise RuntimeError("/api/auth/login 未返回会话 Cookie")
    workspace = workspace_id or login.get("active_workspace_id")
    if not isinstance(workspace, str) or not workspace:
        raise RuntimeError("/api/auth/login 未返回活动工作空间")

    query = urlencode({"workspace_id": workspace})
    bootstrap = request_json(opener, Request(  # noqa: S310 - origin is restricted to HTTPS
        f"{origin}/api/bootstrap?{query}", headers={"Accept": "application/json"},
    ))
    active = bootstrap.get("active_workspace")
    if not isinstance(active, dict) or active.get("id") != workspace or not isinstance(bootstrap.get("skills"), list):
        raise RuntimeError("/api/bootstrap 未返回所选工作空间及技能列表")
    return workspace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Public HTTPS root URL")
    parser.add_argument("--workspace-id", help="Workspace to check (default: login's active workspace)")
    args = parser.parse_args()
    username = os.getenv("MERIDIAN_DEPLOY_USERNAME", "").strip()
    password = os.getenv("MERIDIAN_DEPLOY_PASSWORD")
    if not username:
        parser.error("请设置 MERIDIAN_DEPLOY_USERNAME")
    if password is None and sys.stdin.isatty():
        password = getpass.getpass("部署验证账号密码：")
    if not password:
        parser.error("请交互输入密码，或通过密钥管理工具注入 MERIDIAN_DEPLOY_PASSWORD")
    try:
        workspace = verify_deployment(args.url, username, password, args.workspace_id)
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"部署验证失败：{error}\n")
    print(f"部署验证通过，登录及 /api/bootstrap 均返回 HTTP 200，工作空间 {workspace}")


if __name__ == "__main__":
    main()
