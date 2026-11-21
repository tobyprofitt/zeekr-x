"""Save a session from the owner's Zeekr app using mitmdump."""

from __future__ import annotations

import os

from mitmproxy import http

from zeekr_x_api.session_cache import load_env, session_path, write_session


class ZeekrTokenCapture:
    def __init__(self) -> None:
        load_env()
        self.path = session_path("main")
        self.saved = False
        print("Waiting for a successful vehicle request from your Zeekr app…")

    def response(self, flow: http.HTTPFlow) -> None:
        if self.saved or not flow.response or not 200 <= flow.response.status_code < 300:
            return

        host = flow.request.pretty_host.rstrip(".").lower()
        if not host.endswith((".zeekrlife.com", ".zeekr.eu")) or "tsp-api-gw" not in host:
            return

        auth = flow.request.headers.get("authorization")
        if not auth:
            return

        session = {
            "username": os.environ.get("ZEEKR_MAIN_USERNAME", ""),
            "password": os.environ.get("ZEEKR_MAIN_PASSWORD", ""),
            "country_code": os.environ.get("ZEEKR_COUNTRY_CODE") or "AU",
            "auth_token": auth,
            "bearer_token": auth,
            "device_id": flow.request.headers.get("x-device-id", ""),
            "user_info": {},
            "app_server_host": os.environ.get("ZEEKR_APP_SERVER_HOST", ""),
            "usercenter_host": os.environ.get("ZEEKR_USERCENTER_HOST", ""),
            "message_host": os.environ.get("ZEEKR_MESSAGE_HOST", ""),
            "region_code": os.environ.get("ZEEKR_REGION_CODE") or "SEA",
            "region_login_server": f"https://{host}/",
        }
        write_session(self.path, session)
        self.saved = True
        print(f"Saved {self.path}. You can stop mitmdump now.")


addons = [ZeekrTokenCapture()]
