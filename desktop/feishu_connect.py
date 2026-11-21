"""Reconnect the managed Feishu CLI from the Windows user's interactive desktop."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from aster.feishu.transport import FeishuEventTransport, FeishuTransportError
from aster.paths import resolve_data_dir
from aster.platform.filesystem import FileLock
from aster.tools.feishu import _json_envelope


class ConnectionError(RuntimeError):
    """A message safe to display in the operator's console."""


async def identity_status(transport, profile):
    raw = json.loads(await transport._command(profile, "auth", "status"))
    if (not isinstance(raw, dict) or raw.get("appId") != profile.app_id or raw.get("brand") != profile.brand
            or raw.get("credentialSource") != "local"):
        raise ConnectionError("CLI 报告的应用与 Aster 的受管配置不一致。请核对数据目录。")
    identities = raw.get("identities")
    bot = identities.get("bot") if isinstance(identities, dict) else None
    user = identities.get("user") if isinstance(identities, dict) else None
    if (not isinstance(bot, dict) or not isinstance(user, dict) or type(bot.get("available")) is not bool
            or type(user.get("available")) is not bool or user.get("openId") not in (None, "", profile.user_open_id)):
        raise ConnectionError("CLI 返回了无法核对的身份信息。请检查原有应用和账号。")
    return bot, user, raw.get("dpopMode")


def verify_entered_secret(app_id, brand, secret):
    host = {"feishu": "accounts.feishu.cn", "lark": "accounts.larksuite.com"}[brand]
    # v1.0.100's config init probes this same client_credentials endpoint
    # after writing the keychain. Validate first and discard the response.
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=12) as client:
            response = client.post(f"https://{host}/oauth/v3/token",
                                   data={"grant_type": "client_credentials", "client_id": app_id,
                                         "client_secret": secret})
            status = response.json() if response.status_code == 200 else {}
            valid = (isinstance(status, dict) and not status.get("error") and status.get("code", 0) == 0
                     and isinstance(status.get("access_token"), str) and bool(status["access_token"].strip()))
    except (httpx.HTTPError, ValueError):
        valid = False
    if not valid:
        raise ConnectionError("飞书未确认输入的应用密钥，已保留当前配置。请核对原应用的密钥及网络。")


def run_cli(transport, profile, args, *, cwd, env, timeout, **streams):
    # Synchronous child ownership is deliberate in this single-user console:
    # subprocess.run waits for termination on Ctrl+C before releasing the lock.
    transport._require_generation(profile.generation)
    result = subprocess.run([str(profile.executable), *args], cwd=cwd, env=env, timeout=timeout, check=False, **streams)
    transport._require_generation(profile.generation)
    return result


async def needs_bot_secret(transport, profile):
    bot, _, _ = await identity_status(transport, profile)
    if not bot["available"]:
        if (bot.get("status") == "not_configured" and
                bot.get("message") == "Bot identity: not configured (missing app secret or bot token)"):
            return True
        raise ConnectionError("当前身份策略或应用配置阻止机器人访问。请核对原应用与身份限制。")
    # A stale or incorrectly entered secret can still produce local 'ready'.
    # Only a typed invalid_client rejection authorizes replacing such a binding.
    result = run_cli(transport, profile, ["api", "GET", "/open-apis/bot/v3/info", "--as", "bot",
                                          f"--profile={profile.app_id}"], cwd=transport.data_dir,
                     env=transport._environment(), stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
    if result.returncode:
        envelope = (_json_envelope(result.stderr.decode("utf-8", errors="replace"))
                    or _json_envelope(result.stdout.decode("utf-8", errors="replace")) or {})
        error = envelope.get("error") if envelope.get("ok") is False else None
        return isinstance(error, dict) and (error.get("type"), error.get("subtype")) == ("config", "invalid_client")
    return False


async def connect_bot(transport, profile):
    await transport._verify_version(profile)
    if not await needs_bot_secret(transport, profile):
        return

    print(f"此 Windows 桌面需要重新连接原应用 {profile.app_id} 的机器人。\n"
          "请在飞书开放平台找到同一应用的 App Secret，在本窗口输入，或按 Ctrl+C 取消。", flush=True)
    secret = getpass.getpass("原应用的 App Secret（输入时隐藏） ")
    if not secret or len(secret) > 4096 or secret != secret.strip() or any(c in secret for c in "\0\r\n"):
        raise ConnectionError("请输入原应用的完整密钥。")
    try:
        with FileLock(transport.data_dir / ".feishu-connect.lock", timeout=1):
            transport._require_generation(profile.generation)
            if not await needs_bot_secret(transport, profile):
                return
            verify_entered_secret(profile.app_id, profile.brand, secret)
            # v1.0.100 indexes DPAPI credentials by app ID. Isolate config init's
            # single-app rewrite from the operator's profiles and group bindings.
            with tempfile.TemporaryDirectory(prefix="aster-feishu-connect-") as scratch:
                env = transport._environment()
                env["LARKSUITE_CLI_CONFIG_DIR"] = scratch
                result = run_cli(transport, profile, ["config", "init", "--app-id", profile.app_id, "--brand",
                                                       profile.brand, "--app-secret-stdin"], cwd=scratch, env=env,
                                 input=(secret + "\n").encode("utf-8"), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=55)
                if result.returncode:
                    raise ConnectionError("官方 CLI 未能验证或保存应用密钥，请核对应用与网络后重试。")
    finally:
        del secret
    transport._require_generation(profile.generation)
    bot, _, _ = await identity_status(transport, profile)
    if not bot["available"]:
        raise ConnectionError("CLI 仍无法使用原应用的机器人凭据。请从日常打开 Aster 的 Windows 桌面重试。")


def requested_scopes(value, file=None):
    if file is not None:
        if not file.is_file():
            raise ConnectionError("请提供仅含权限名称的文本或 JSON 文件。")
        with file.open("rb") as source:
            content = source.read(65537)
        if len(content) > 65536:
            raise ConnectionError("权限列表文件超过 64 KiB。")
        value = content.decode("utf-8-sig").strip()
        if value.startswith(("[", "{")):
            value = json.loads(value)
            if isinstance(value, dict):
                value = value.get("scopes")
            if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
                raise ConnectionError("JSON 权限文件需要包含权限名称列表。")
            value = " ".join(value)
    names = list(dict.fromkeys(value.replace(",", " ").split()))
    if (not names or len(" ".join(names)) > 24000 or any(name != "offline_access" and not re.fullmatch(
            r"[a-z][a-z0-9_.]*(?::[a-z0-9_.]+)+", name) for name in names)):
        raise ConnectionError("请输入完整的飞书权限名称，多个权限可用空格或换行分隔。")
    return names


def has_user_scopes(user, profile, scopes):
    granted = user.get("scope")
    return (user["available"] and user.get("openId") == profile.user_open_id and isinstance(granted, str)
            and set(scopes).issubset(granted.split()))


async def connect_user(transport, profile, scopes):
    if not profile.user_open_id:
        raise ConnectionError("受管配置未绑定原个人账号。请先在 Aster 中核对要使用的账号。")
    _, user, _ = await identity_status(transport, profile)
    if has_user_scopes(user, profile, scopes):
        print("原个人账号的授权已经生效，已核对指定权限。")
        return
    print(f"请在飞书授权页使用原账号（Open ID 尾号 {profile.user_open_id[-6:]}），此次申请 {len(scopes)} 项权限。")
    print(" ".join(scopes))
    if input("使用这个范围启动个人授权？[y/N] ").strip().lower() not in ("y", "yes", "是"):
        raise ConnectionError("已取消个人授权。")
    with FileLock(transport.data_dir / ".feishu-connect.lock", timeout=1):
        transport._require_generation(profile.generation)
        _, user, dpop = await identity_status(transport, profile)
        if has_user_scopes(user, profile, scopes):
            print("原个人账号的授权已经生效，已核对指定权限。")
            return
        strict = (await transport._command(profile, "config", "strict-mode")).decode("utf-8").strip()
        mode = re.fullmatch(r"strict-mode: (off|user) \(source: .+\)", strict)
        if not mode or dpop not in ("preferred", "required", "disabled"):
            raise ConnectionError("当前配置未允许个人登录，或无法核对身份策略。")
        # Login replaces the CLI profile's user list and removes the previous
        # user's token. A scratch profile with no users protects the original
        # binding if a different account accepts the device authorization.
        with tempfile.TemporaryDirectory(prefix="aster-feishu-login-") as scratch:
            env = transport._environment()
            env["LARKSUITE_CLI_CONFIG_DIR"] = scratch
            app = {"appId": profile.app_id, "brand": profile.brand, "strictMode": mode[1], "dpopMode": dpop,
                   "appSecret": {"source": "keychain", "id": f"appsecret:{profile.app_id}"}, "users": []}
            config_path = Path(scratch) / "config.json"
            config_path.write_text(json.dumps({"currentApp": profile.app_id, "apps": [app]}), encoding="utf-8")
            # Use the operator's own console for the official verification URL
            # and user code. The CLI alone stores the resulting credentials.
            result = run_cli(transport, profile, ["auth", "login", "--scope", " ".join(scopes),
                                                  f"--profile={profile.app_id}"], cwd=scratch, env=env, timeout=900)
            if result.returncode:
                raise ConnectionError("官方 CLI 未完成本次个人授权，请查看本窗口中的飞书返回信息。")
            bound = json.loads(config_path.read_text(encoding="utf-8"))["apps"][0].get("users")
            if (not isinstance(bound, list) or len(bound) != 1 or not isinstance(bound[0], dict)
                    or bound[0].get("userOpenId") != profile.user_open_id):
                raise ConnectionError("完成授权的账号和原账号不一致。Aster 保留了原有账号绑定。")
        transport._require_generation(profile.generation)
        _, user, _ = await identity_status(transport, profile)
        if not has_user_scopes(user, profile, scopes):
            raise ConnectionError("原账号仍缺少本次指定的部分权限，请核对飞书授权页面上的实际授予范围。")
    print("原个人账号已连接，已在 Aster 的受管配置中核对指定权限。")


async def connect(data_dir, bot_only=False, scopes=None):
    transport = FeishuEventTransport(data_dir)
    profile = transport._profile()
    await connect_bot(transport, profile)
    transport._require_generation(profile.generation)
    discovered = await transport.discover()
    if discovered["identity"]["generation"] != profile.generation:
        raise ConnectionError("检查期间 Aster 选用的飞书配置已变化，请重新打开连接工具。")
    print(f"机器人连接成功，已验证飞书 API 访问，可读取 {len(discovered['groups'])} 个群。")
    if bot_only:
        return
    if scopes is None:
        value = input("要恢复个人授权，请输入权限名称或 @权限列表文件路径。仅检查机器人请按 Enter。 ").strip()
        if not value:
            return
        scopes = requested_scopes(value, Path(value[1:]) if value.startswith("@") else None)
    await connect_user(transport, profile, scopes)


def launch_terminal(data_dir, bot_only):
    script = Path(__file__).resolve(strict=True)
    command = [sys.executable, "-I", "-X", "utf8", str(script), "--data-dir", str(data_dir), "--pause"]
    if bot_only:
        command.append("--bot-only")
    # Omit STARTF_USESTDHANDLES: Windows assigns all three streams to the
    # newly created console rather than inheriting Electron's launch pipe.
    subprocess.Popen(command, cwd=script.parent, close_fds=True, creationflags=subprocess.CREATE_NEW_CONSOLE)


def main(argv=None):
    parser = argparse.ArgumentParser(description="在当前 Windows 桌面恢复 Aster 的飞书连接。")
    parser.add_argument("--data-dir", type=Path, help="Aster 数据目录")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--bot-only", action="store_true", help="只验证和恢复原应用机器人")
    selection.add_argument("--scope", action="append", help="指定个人授权的权限名称，可重复使用")
    selection.add_argument("--scopes-file", type=Path, help="仅含个人授权权限名称的文本或 JSON 文件")
    parser.add_argument("--launch-terminal", action="store_true", help="从桌面菜单打开独立交互窗口")
    parser.add_argument("--pause", action="store_true", help="检查结束后等待按 Enter")
    args = parser.parse_args(argv)
    if args.launch_terminal:
        if args.scope or args.scopes_file:
            parser.error("请在新建的本地交互窗口中选择个人权限。")
        if os.name != "nt":
            print("此连接工具需要 Windows。", file=sys.stderr)
            return 2
        try:
            launch_terminal(resolve_data_dir(args.data_dir), args.bot_only)
        except (OSError, ValueError):
            print("无法打开本地飞书连接窗口。", file=sys.stderr)
            return 1
        print(json.dumps({"launched": True}))
        return 0
    if os.name != "nt" or not all(stream is not None and stream.isatty()
                                  for stream in (sys.stdin, sys.stdout, sys.stderr)):
        print("请从 Windows 桌面的 Aster 菜单或交互式终端打开此工具。", file=sys.stderr)
        return 2
    try:
        scopes = requested_scopes(" ".join(args.scope or []), args.scopes_file) if args.scope or args.scopes_file else None
        asyncio.run(connect(resolve_data_dir(args.data_dir), args.bot_only, scopes))
        return 0
    except (ConnectionError, FeishuTransportError) as exc:
        print(str(exc), file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print("已取消连接操作。", file=sys.stderr)
    except (OSError, TimeoutError, ValueError, subprocess.SubprocessError):
        print("无法完成飞书连接。请核对受管 CLI、配置目录和网络连接。", file=sys.stderr)
    finally:
        if args.pause:
            try:
                input("按 Enter 关闭窗口。")
            except (EOFError, KeyboardInterrupt):
                pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
