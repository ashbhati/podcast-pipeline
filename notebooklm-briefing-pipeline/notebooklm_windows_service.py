#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "logs"
LOG_PATH = LOG_DIR / "windows_service.log"
CLOUDFLARED_CONFIG = ROOT / "cloudflared" / "config.yml"
DEFAULT_PUBLIC_URL = "https://podcast.example.com"
SERVICE_NAME = "NotebookLMPodcastPipeline"


class ManagedProcess:
    def __init__(self, name: str, command: list[str], cwd: Path, env: dict[str, str]):
        self.name = name
        self.command = command
        self.cwd = cwd
        self.env = env
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if self.is_running():
            return
        logging.info("starting %s: %s", self.name, self.command)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            env=self.env,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 15.0) -> None:
        if not self.proc:
            return
        if self.proc.poll() is not None:
            self.proc = None
            return
        logging.info("stopping %s pid=%s", self.name, self.proc.pid)
        try:
            self.proc.terminate()
            self.proc.wait(timeout=timeout)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        finally:
            self.proc = None


class NotebookLMPodcastPipelineService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = "NotebookLM Podcast Pipeline"
    _svc_description_ = "Runs the local NotebookLM podcast bridge and Cloudflare tunnel before user login."

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.bridge: ManagedProcess | None = None
        self.tunnel: ManagedProcess | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        self._stop_children()

    def SvcDoRun(self):
        _init_logging()
        servicemanager.LogInfoMsg(f"{SERVICE_NAME} starting")
        logging.info("service starting")
        try:
            self._run_loop()
        except Exception as exc:
            logging.exception("service crashed: %s", exc)
            servicemanager.LogErrorMsg(f"{SERVICE_NAME} crashed: {exc}")
            raise
        finally:
            self._stop_children()
            logging.info("service stopped")
            servicemanager.LogInfoMsg(f"{SERVICE_NAME} stopped")

    def _run_loop(self) -> None:
        child_env = _child_env()
        _ensure_public_config()
        bridge_cmd = [sys.executable, str(ROOT / "podcast_bridge.py")]
        tunnel_cmd = [
            _cloudflared_exe(),
            "tunnel",
            "--config",
            str(CLOUDFLARED_CONFIG),
            "run",
            "notebooklm-podcast",
        ]
        self.bridge = ManagedProcess("podcast-bridge", bridge_cmd, ROOT, child_env)
        self.tunnel = ManagedProcess("cloudflared-tunnel", tunnel_cmd, ROOT / "cloudflared", child_env)

        self.bridge.start()
        self.tunnel.start()
        self._wait_for_bridge_health(timeout=30)

        while True:
            signaled = win32event.WaitForSingleObject(self.stop_event, 15000)
            if signaled == win32event.WAIT_OBJECT_0:
                break

            _ensure_public_config()

            if not self.tunnel.is_running():
                logging.warning("cloudflared exited; restarting")
                self.tunnel.start()

            if not self.bridge.is_running() or not _bridge_healthy():
                logging.warning("bridge unhealthy; restarting")
                self.bridge.stop(timeout=5)
                self.bridge.start()
                self._wait_for_bridge_health(timeout=30)

    def _wait_for_bridge_health(self, timeout: int) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _bridge_healthy():
                logging.info("bridge healthy")
                return
            time.sleep(2)
        logging.warning("bridge did not become healthy within %ss", timeout)

    def _stop_children(self) -> None:
        if self.bridge:
            self.bridge.stop()
        if self.tunnel:
            self.tunnel.stop()


def _init_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    return {}


def _save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")


def _ensure_public_config() -> None:
    config = _load_config()
    bridge_cfg = config.setdefault("podcast_bridge", {})
    changed = False
    if bridge_cfg.get("base_url") != DEFAULT_PUBLIC_URL:
        bridge_cfg["base_url"] = DEFAULT_PUBLIC_URL
        changed = True
    image_url = f"{DEFAULT_PUBLIC_URL}/static/cover.png"
    if bridge_cfg.get("image_url") != image_url:
        bridge_cfg["image_url"] = image_url
        changed = True
    if changed:
        _save_config(config)
        logging.info("updated config base_url/image_url to %s", DEFAULT_PUBLIC_URL)


def _bridge_healthy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8788/healthz", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _cloudflared_exe() -> str:
    candidates = [
        ROOT / "cloudflared" / "cloudflared.exe",
        Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
        Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("cloudflared.exe not found")


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    user_home = str(ROOT.parents[2])
    env["USERPROFILE"] = user_home
    env["HOME"] = user_home
    env["HOMEDRIVE"] = Path(user_home).drive
    env["HOMEPATH"] = str(Path(user_home)).replace(Path(user_home).drive, "", 1)
    env["APPDATA"] = str(Path(user_home) / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(Path(user_home) / "AppData" / "Local")
    return env


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(NotebookLMPodcastPipelineService)

