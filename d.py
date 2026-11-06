# ==== Marimo cell: paste this whole thing into ONE Python cell and run it ====
import subprocess, sys, time
from pathlib import Path

SCRIPT = r'''#!/usr/bin/env python3
# Supervisor: 3x-ui + ngrok + backpack (Kharej) without systemd.
import fcntl
import logging
import os
import platform
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

# ===================== CONFIG =====================
NGROK_TOKEN = "3LF9gELydLL1jmogTFkTQtVgBIr_3S7fnd2ZDN44RuDEZuJZG"
NGROK_DOMAIN = "sevenfold-footbath-national.ngrok-free.dev"
BACKPACK_LINK = "backpack://2.AAEAAQECAwRAITqlfmz8tTSNRelOA4jDAdVqAOMyb_3v5vUvQRd8542YcGjspcVwYfPa8910fyvsBAEMBwPSCQgCXrfP0QkBFAoD0wkQFBEGHQQf_k8"
PANEL_PORT = 2053
PANEL_USER = "dada"
PANEL_PASS = "dada"
PANEL_BASE_PATH = "/"
BACKPACK_CONF_DIR = "/etc/backpack"
BACKPACK_RUN_CMD = ""  # optional manual override, e.g. "backpack -c /etc/backpack/xxx.toml"
CHECK_INTERVAL = 15
# ==================================================

BASE = Path.home() / "tunnel"
BIN_DIR = BASE / "bin"
LOG_DIR = BASE / "logs"
XUI_DIR = BASE / "x-ui"
XUI_DB_DIR = BASE / "xui-db"
for d in (BIN_DIR, LOG_DIR, XUI_DB_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_DIR / "supervisor.log")],
)
log = logging.getLogger("supervisor")

ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
    platform.machine().lower(), "amd64"
)
XUI_ENV = {
    **os.environ,
    "XUI_DB_FOLDER": str(XUI_DB_DIR),
    "XUI_BIN_FOLDER": str(XUI_DIR / "bin"),
    "XUI_LOG_FOLDER": str(LOG_DIR),
}


def download(url, dest):
    log.info("downloading %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def install_ngrok():
    exe = BIN_DIR / "ngrok"
    if not exe.exists():
        tgz = BASE / "ngrok.tgz"
        download("https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-%s.tgz" % ARCH, tgz)
        with tarfile.open(tgz) as t:
            t.extract("ngrok", BIN_DIR)
        tgz.unlink(missing_ok=True)
        exe.chmod(0o755)
    subprocess.run([str(exe), "config", "add-authtoken", NGROK_TOKEN],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install_xui():
    exe = XUI_DIR / "x-ui"
    if not exe.exists():
        tgz = BASE / "x-ui.tar.gz"
        download("https://github.com/MHSanaei/3x-ui/releases/latest/download/x-ui-linux-%s.tar.gz" % ARCH, tgz)
        with tarfile.open(tgz) as t:
            t.extractall(BASE)
        tgz.unlink(missing_ok=True)
        exe.chmod(0o755)
        for f in (XUI_DIR / "bin").glob("xray-linux-*"):
            f.chmod(0o755)

    def run(args):
        return subprocess.run([str(exe), "setting"] + args, cwd=XUI_DIR, env=XUI_ENV,
                              capture_output=True, text=True)

    run(["-username", PANEL_USER, "-password", PANEL_PASS, "-port", str(PANEL_PORT)])
    run(["-webBasePath", PANEL_BASE_PATH])
    shown = run(["-show", "true"])
    log.info("x-ui settings:\n%s", (shown.stdout + shown.stderr).strip())


def find_backpack_conf():
    confs = sorted(Path(BACKPACK_CONF_DIR).glob("*.toml"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return confs[0] if confs else None


def install_backpack():
    if shutil.which("backpack") and find_backpack_conf():
        return
    log.info("installing backpack + applying link")
    cmd = ("bash <(curl -fsSL https://raw.githubusercontent.com/AminMGMT/BackPack/main/install.sh) "
           "link apply " + shlex.quote(BACKPACK_LINK))
    # Without systemd this may end with 'exit status 1'; that is fine as long as
    # the binary and the .toml config exist afterwards.
    subprocess.run(cmd, shell=True, executable="/bin/bash", check=False, input=b"\n\n\n",
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("backpack config: %s", find_backpack_conf() or "NOT FOUND")


def backpack_cmd():
    if BACKPACK_RUN_CMD:
        return shlex.split(BACKPACK_RUN_CMD.replace("nohup ", "").rstrip(" &"))
    conf = find_backpack_conf()
    if conf and shutil.which("backpack"):
        return ["backpack", "-c", str(conf)]
    return None


class Proc:
    def __init__(self, name, cmd, cwd=None, env=None):
        self.name, self.cmd, self.cwd, self.env = name, cmd, cwd, env
        self.p = None
        self.last_start = 0.0
        self.fails = 0

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def ensure(self):
        if self.alive():
            return
        if min(60, 2 ** self.fails) - (time.time() - self.last_start) > 0:
            return  # backoff
        logf = open(LOG_DIR / (self.name + ".log"), "ab")
        log.info("starting %s: %s", self.name, " ".join(map(str, self.cmd)))
        self.fails = self.fails + 1 if time.time() - self.last_start < 30 else 0
        self.last_start = time.time()
        self.p = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdout=logf,
                                  stderr=subprocess.STDOUT, start_new_session=True)

    def stop(self):
        if self.alive():
            self.p.terminate()


def main():
    lock = open(BASE / "supervisor.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another instance is already running; exiting.")
        return

    for step in (install_ngrok, install_xui, install_backpack):
        try:
            step()
        except Exception:
            log.exception("%s failed (will continue)", step.__name__)

    procs = {
        "xui": Proc("xui", [str(XUI_DIR / "x-ui")], cwd=XUI_DIR, env=XUI_ENV),
        "ngrok": Proc("ngrok", [str(BIN_DIR / "ngrok"), "http",
                                "--domain=" + NGROK_DOMAIN, str(PANEL_PORT)]),
    }

    def shutdown(*_):
        for p in procs.values():
            p.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    last_bp_try = time.time()
    while True:
        try:
            if "backpack" not in procs:
                bp = backpack_cmd()
                if bp:
                    procs["backpack"] = Proc("backpack", bp)
                elif time.time() - last_bp_try > 300:  # retry install every 5 min
                    last_bp_try = time.time()
                    log.warning("backpack config not found; retrying install (check /etc/backpack)")
                    install_backpack()
            for p in procs.values():
                p.ensure()
        except Exception:
            log.exception("loop error")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
'''

home = Path.home() / "tunnel"
home.mkdir(parents=True, exist_ok=True)
script_path = home / "supervisor.py"
script_path.write_text(SCRIPT)

# Run detached so the cell returns immediately and the supervisor keeps running.
# If it is already running, the lock file makes the new copy exit harmlessly.
launcher_log = open(home / "launcher.log", "ab")
subprocess.Popen([sys.executable, str(script_path)], stdout=launcher_log,
                 stderr=subprocess.STDOUT, start_new_session=True)

time.sleep(8)
print("Supervisor started. Panel: https://sevenfold-footbath-national.ngrok-free.dev  (user: dada / pass: dada)")
print("--- last log lines ---")
log_file = home / "logs" / "supervisor.log"
if log_file.exists():
    print("".join(log_file.read_text().splitlines(True)[-25:]))
