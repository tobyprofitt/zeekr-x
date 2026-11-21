"""Load .env and reuse a saved Zeekr session so we do not log in on every run.

Session files live in the private config directory. They include tokens and
the password, because the library needs the password to refresh a token.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import ZeekrClient
from .exceptions import AuthException, ZeekrException

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
CONFIG_DIR = Path(os.environ.get("ZEEKR_CONFIG_DIR") or CONFIG_HOME / "zeekr-x").expanduser()
ENV_FILE = CONFIG_DIR / ".env"

SECRET_KEYS = (
    "ZEEKR_HMAC_ACCESS_KEY",
    "ZEEKR_HMAC_SECRET_KEY",
    "ZEEKR_PASSWORD_PUBLIC_KEY",
    "ZEEKR_PROD_SECRET",
    "ZEEKR_VIN_KEY",
    "ZEEKR_VIN_IV",
)

ACCOUNT_KEYS = {
    "spare": ("ZEEKR_USERNAME", "ZEEKR_PASSWORD"),
    "main": ("ZEEKR_MAIN_USERNAME", "ZEEKR_MAIN_PASSWORD"),
}


def load_env(path: Path | None = None) -> None:
    """Reads KEY=VALUE lines from .env into os.environ."""
    env_path = path or ENV_FILE
    if not env_path.exists():
        raise FileNotFoundError(f"Missing {env_path}. See README.md for setup.")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def session_path(account: str) -> Path:
    return CONFIG_DIR / f"session-{account}.json"


def _read_session(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("bearer_token"):
        return None
    return data


def write_session(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(payload, file, indent=2)
            file.write("\n")
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _save_client_session(path: Path, client: ZeekrClient) -> None:
    payload = client.export_session()
    if payload:
        write_session(path, payload)


def _build_client(username: str, password: str, session_data: dict | None) -> ZeekrClient:
    return ZeekrClient(
        username=username,
        password=password,
        country_code=os.environ.get("ZEEKR_COUNTRY_CODE", "AU"),
        hmac_access_key=os.environ["ZEEKR_HMAC_ACCESS_KEY"],
        hmac_secret_key=os.environ["ZEEKR_HMAC_SECRET_KEY"],
        password_public_key=os.environ["ZEEKR_PASSWORD_PUBLIC_KEY"],
        prod_secret=os.environ["ZEEKR_PROD_SECRET"],
        vin_key=os.environ["ZEEKR_VIN_KEY"],
        vin_iv=os.environ["ZEEKR_VIN_IV"],
        session_data=session_data,
    )


def connect(
    account: str = "spare",
    force_login: bool = False,
    allow_login: bool = False,
) -> ZeekrClient:
    """Return a client from a saved session file.

    account: "spare" (bot) or "main" (owner, for trip history).
    force_login: ignore the saved file. Needs allow_login=True.
    allow_login: if True, try the password login API when there is no usable
    saved session. See README.md if the server returns 9300.
    """
    if account not in ACCOUNT_KEYS:
        raise ValueError('account must be "spare" or "main"')

    load_env()
    missing = [key for key in SECRET_KEYS if not os.environ.get(key)]
    user_key, pass_key = ACCOUNT_KEYS[account]
    if not os.environ.get(user_key) or not os.environ.get(pass_key):
        missing.extend([user_key, pass_key])
    if missing:
        raise SystemExit("Missing values in .env: " + ", ".join(dict.fromkeys(missing)))

    username = os.environ[user_key]
    password = os.environ[pass_key]
    path = session_path(account)
    force_login = force_login or os.environ.get("ZEEKR_FORCE_LOGIN", "").lower() in {
        "1",
        "true",
        "yes",
    }
    allow_login = allow_login or force_login

    saved = None if force_login else _read_session(path)
    if saved and saved.get("username") and saved["username"] != username:
        print(
            f"Session username differs from {user_key}. "
            "Using the captured token anyway."
        )
        saved["username"] = username
        saved["password"] = password

    client = _build_client(username, password, saved)

    if saved and client.logged_in:
        try:
            client.get_vehicle_list()
            _save_client_session(path, client)
            print(f"Reused saved {account} session ({path.name}).")
            return client
        except (AuthException, ZeekrException) as exc:
            print(f"Saved {account} session is stale ({exc}).")
            if "079021" in str(exc):
                raise SystemExit(
                    f"Zeekr rejected {path.name}: the {account} account is "
                    "logged in on another device. See README.md for session setup."
                ) from exc

    if not allow_login:
        raise SystemExit(
            f"No working saved session at {path.name}. See README.md for session setup."
        )

    print(f"Logging in as {account} account...")
    client.login(relogin=bool(saved))
    _save_client_session(path, client)
    print(f"Login OK. Session saved to {path.name}.")
    return client


def pick_vehicle(client: ZeekrClient):
    """Return the configured VIN, or the first vehicle on the account."""
    vehicles = client.vehicles or client.get_vehicle_list()
    if not vehicles:
        raise SystemExit("No vehicles on this account.")
    wanted = os.environ.get("ZEEKR_VIN") or vehicles[0].vin
    return next((item for item in vehicles if item.vin == wanted), vehicles[0])
