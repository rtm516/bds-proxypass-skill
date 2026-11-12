#!/usr/bin/env python3
"""BDS + ProxyPass capture environment for Windows and Linux. Standard library only."""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

WINDOWS = os.name == "nt"
MARKER = ".bds-proxypass-env"
BDS_EXE = "bedrock_server.exe" if WINDOWS else "bedrock_server"
DOWNLOAD_BASE = "https://www.minecraft.net/bedrockdedicatedserver"
PROXYPASS_REPO = "https://github.com/CloudburstMC/ProxyPass"
# minecraft.net rejects requests without a browser-like user agent.
USER_AGENT = "Mozilla/5.0"


def default_root():
    return os.environ.get("BDS_PROXYPASS_ROOT") or str(Path(tempfile.gettempdir()) / "bds-proxypass")


def run(what, cmd, **kwargs):
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        sys.exit(f"{what} failed (exit {result.returncode})")


def load_env(root):
    env_file = root / "env.json"
    if not env_file.exists():
        sys.exit(f"No env.json in {root}; run setup first")
    return json.loads(env_file.read_text())


# --- JDK discovery

def java_major(home):
    release = Path(home) / "release"
    if release.exists():
        match = re.search(r'JAVA_VERSION="(\d+)', release.read_text(errors="ignore"))
        if match:
            return int(match.group(1))
    return 0


def java_bin(home):
    return Path(home) / "bin" / ("java.exe" if WINDOWS else "java")


def find_jdks(extra=None):
    """Maps each installed JDK home to its major version."""
    candidates = [extra, os.environ.get("JAVA_HOME")]
    if WINDOWS:
        bases = [r"C:\Program Files\Eclipse Adoptium", r"C:\Program Files\Java",
                 r"C:\Program Files\Microsoft", str(Path.home() / ".jdks")]
    else:
        bases = ["/usr/lib/jvm", "/usr/java", "/opt/java", str(Path.home() / ".jdks"),
                 str(Path.home() / ".sdkman/candidates/java")]
    for base in bases:
        if os.path.isdir(base):
            candidates += [os.path.join(base, name) for name in os.listdir(base)]
    on_path = shutil.which("java")
    if on_path:
        candidates.append(str(Path(on_path).resolve().parent.parent))

    jdks = {}
    for home in candidates:
        if home and java_bin(home).exists() and java_major(home):
            jdks.setdefault(str(Path(home).resolve()), java_major(home))
    return jdks


def pick_runtime_jdk(jdks):
    usable = [home for home in jdks if jdks[home] >= 17]
    # Prefer 21, then the newest of the rest.
    return sorted(usable, key=lambda home: (jdks[home] != 21, -jdks[home]))[0] if usable else None


# --- setup

def download(url, dest):
    part = dest.with_name(dest.name + ".part")
    print(f"Downloading {url}")
    curl = shutil.which("curl")
    if curl:
        run("BDS download", [curl, "-sS", "-L", "--fail", "-A", USER_AGENT, "-o", str(part), url])
    else:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request) as response, open(part, "wb") as out:
            shutil.copyfileobj(response, out)
    part.replace(dest)


def patch_properties(path, settings):
    text = path.read_text(encoding="utf-8")
    for key, value in settings.items():
        line = f"{key}={value}"
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(lambda _: line, text)
        else:
            text = text.rstrip() + f"\n{line}\n"
    path.write_text(text, encoding="utf-8", newline="")


def patch_yaml(path, settings):
    """Sets scalar keys in place, keeping comments. Each setting is (section or None, key, value)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for section, key, value in settings:
        start, end, indent = 0, len(lines), ""
        if section:
            header = next((i for i, line in enumerate(lines) if re.match(rf"{re.escape(section)}:\s*(#.*)?$", line)), None)
            if header is None:
                lines += [f"{section}:"]
                header = len(lines) - 1
            start = end = header + 1
            while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
                end += 1
            indent = "  "
        pattern = re.compile((r"[ \t]+" if section else "") + re.escape(key) + ":")
        entry = f"{indent}{key}: {value}"
        for i in range(start, end):
            if pattern.match(lines[i]):
                lines[i] = entry
                break
        else:
            lines.insert(start if section else len(lines), entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")


def cmd_setup(args):
    if not re.fullmatch(r"\d+(\.\d+){2,3}", args.version):
        sys.exit(f"Version '{args.version}' does not look like a BDS version (e.g. 1.26.63.1)")
    if args.proxy_port == args.server_port:
        sys.exit("--proxy-port and --server-port must differ")

    root = Path(args.root).resolve()
    downloads = root / "downloads"
    platform = "win" if WINDOWS else "linux"
    bds_dir = root / f"bds-{platform}-{args.version}"
    src_dir = root / "ProxyPass"
    run_dir = root / "proxypass-run"
    for directory in (downloads, run_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("Created by the bds-proxypass skill. Safe to delete.\n")

    jdks = find_jdks(args.java_home)
    java_home = str(Path(args.java_home).resolve()) if args.java_home else pick_runtime_jdk(jdks)
    if not java_home or jdks.get(java_home, 0) < 17:
        sys.exit("No JDK 17+ found; pass --java-home")
    print(f"Using JDK at {java_home}")

    # BDS
    channel = f"bin-{platform}" + ("-preview" if args.preview else "")
    zip_name = f"bedrock-server-{args.version}.zip"
    zip_path = downloads / f"{channel}-{zip_name}"
    if not zip_path.exists():
        download(f"{DOWNLOAD_BASE}/{channel}/{zip_name}", zip_path)
    if not (bds_dir / BDS_EXE).exists():
        print(f"Extracting to {bds_dir}")
        bds_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(bds_dir)
    if not WINDOWS:
        (bds_dir / BDS_EXE).chmod(0o755)

    patch_properties(bds_dir / "server.properties", {
        "online-mode": "false",
        "allow-list": "false",
        "transport": args.transport,
        "server-port": args.server_port,
        "server-portv6": args.server_port + 1,
        "server-udp-ports": args.server_port,
        "enable-lan-visibility": "false",
        "gamemode": "creative",
        "allow-cheats": "true",
        "level-name": "proxypass-test",
    })

    # ProxyPass
    if not (src_dir / ".git").exists():
        run("ProxyPass clone", ["git", "clone", "--quiet", PROXYPASS_REPO, str(src_dir)])
    else:
        run("ProxyPass fetch", ["git", "-C", str(src_dir), "fetch", "--quiet", "origin"])
    run("ProxyPass checkout", ["git", "-C", str(src_dir), "checkout", "--quiet", args.proxypass_ref])
    if args.proxypass_ref == "master":
        run("ProxyPass pull", ["git", "-C", str(src_dir), "pull", "--quiet", "--ff-only"])
    commit = subprocess.check_output(["git", "-C", str(src_dir), "rev-parse", "--short", "HEAD"], text=True).strip()

    jar = run_dir / "ProxyPass.jar"
    built_from = run_dir / "built-from.txt"
    if args.rebuild or not jar.exists() or not built_from.exists() or built_from.read_text().strip() != commit:
        print(f"Building ProxyPass @ {commit}")
        build_env = dict(os.environ, JAVA_HOME=java_home)
        # The build pins a toolchain version and Gradle cannot download one itself here.
        toolchain = re.search(r"JavaLanguageVersion\.of\((\d+)\)", (src_dir / "build.gradle.kts").read_text(encoding="utf-8"))
        toolchain_arg = []
        if toolchain:
            wanted = int(toolchain.group(1))
            matching = [home for home in jdks if jdks[home] == wanted]
            if not matching:
                sys.exit(f"ProxyPass builds with a JDK {wanted} toolchain and none is installed; install JDK {wanted}")
            toolchain_arg = [f"-Porg.gradle.java.installations.paths={matching[0]}"]
        if WINDOWS:
            gradle = ["cmd", "/c", str(src_dir / "gradlew.bat")]
        else:
            (src_dir / "gradlew").chmod(0o755)
            gradle = ["./gradlew"]
        run("ProxyPass build", gradle + toolchain_arg + ["--quiet", "--console=plain", "shadowJar"], cwd=src_dir, env=build_env)
        shutil.copyfile(src_dir / "build" / "libs" / "ProxyPass.jar", jar)
        built_from.write_text(commit + "\n")

    main_class = src_dir / "src/main/java/org/cloudburstmc/proxypass/ProxyPass.java"
    codec_match = re.search(r"BedrockCodec CODEC = (\w+)\.CODEC", main_class.read_text(encoding="utf-8"))
    codec = codec_match.group(1) if codec_match else "unknown"

    # Start from ProxyPass's own default config and only change what the environment needs.
    config = run_dir / "config.yml"
    if not config.exists():
        shutil.copyfile(src_dir / "src/main/resources/config.yml", config)
    patch_yaml(config, [
        (None, "transport", args.transport),
        ("proxy", "host", "0.0.0.0"),
        ("proxy", "port", args.proxy_port),
        ("destination", "host", "127.0.0.1"),
        ("destination", "port", args.server_port),
        ("nethernet", "verify-client-authentication", "false"),
        (None, "log-packets", "true"),
        (None, "log-to", "file"),
    ])

    (root / "env.json").write_text(json.dumps({
        "version": args.version,
        "preview": args.preview,
        "transport": args.transport,
        "proxyPort": args.proxy_port,
        "serverPort": args.server_port,
        "bdsDir": str(bds_dir),
        "proxyRunDir": str(run_dir),
        "proxyJar": str(jar),
        "javaHome": java_home,
        "proxyCommit": commit,
        "proxyCodec": codec,
    }, indent=2) + "\n")

    print()
    print(f"Environment ready in {root}")
    print(f"  BDS {args.version} -> 127.0.0.1:{args.server_port} ({args.transport}, offline)")
    print(f"  ProxyPass {commit} -> 0.0.0.0:{args.proxy_port} (codec {codec})")
    print(f"  Packet logs -> {run_dir / 'sessions'}")


# --- start

def pid_file(root, name):
    return root / "run" / f"{name}.pid"


def track(root, name, process):
    path = pid_file(root, name)
    path.parent.mkdir(exist_ok=True)
    path.write_text(str(process.pid))
    return path


def stop_child(process):
    if process.poll() is None:
        process.kill()
        process.wait()


def cmd_start_bds(args):
    root = Path(args.root).resolve()
    bds_dir = Path(load_env(root)["bdsDir"])
    child_env = dict(os.environ)
    if not WINDOWS:
        child_env["LD_LIBRARY_PATH"] = str(bds_dir)
    process = subprocess.Popen([str(bds_dir / BDS_EXE)], cwd=bds_dir, env=child_env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", bufsize=1)
    pid_path = track(root, "bds", process)

    def send(command):
        try:
            process.stdin.write(command.rstrip("\r\n") + "\n")
            process.stdin.flush()
        except (OSError, ValueError):
            pass

    def forward_console():
        for line in sys.stdin:
            send(line)

    threading.Thread(target=forward_console, daemon=True).start()

    sent = not args.on_first_join
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            if not sent and "Player Spawned" in line:
                print(f"[start-bds] > {args.on_first_join}", flush=True)
                send(args.on_first_join)
                sent = True
    except KeyboardInterrupt:
        pass
    finally:
        stop_child(process)
        pid_path.unlink(missing_ok=True)


def cmd_start_proxypass(args):
    root = Path(args.root).resolve()
    cfg = load_env(root)
    # ProxyPass reads config.yml and writes sessions/ relative to the working directory.
    process = subprocess.Popen([str(java_bin(cfg["javaHome"])), "--enable-native-access=ALL-UNNAMED",
                                "-jar", cfg["proxyJar"]], cwd=cfg["proxyRunDir"])
    pid_path = track(root, "proxypass", process)
    try:
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_child(process)
        pid_path.unlink(missing_ok=True)


# --- teardown

def process_name(pid):
    """Executable name of a running pid, or None if it is not running."""
    if WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout.strip()
        return out.split(",")[0].strip('"').lower() if out.startswith('"') else None
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe")).lower()
    except OSError:
        return None


def kill(pid):
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def remove_tree(path):
    def retry_writable(function, target, _):
        os.chmod(target, 0o700)
        function(target)

    shutil.rmtree(path, onerror=retry_writable)


def cmd_teardown(args):
    root = Path(args.root).resolve()
    if not root.exists():
        print(f"Nothing at {root}")
        return

    # Only kill pids recorded by the start commands, and only if they are still that program.
    expected = {"bds": ("bedrock_server",), "proxypass": ("java",)}
    stopped = False
    for name, programs in expected.items():
        path = pid_file(root, name)
        if not path.exists():
            continue
        try:
            pid = int(path.read_text().strip())
        except ValueError:
            pid = None
        running = process_name(pid) if pid else None
        if running and running.startswith(programs):
            print(f"Stopping {name} (pid {pid})")
            kill(pid)
            stopped = True
        path.unlink(missing_ok=True)
    if not stopped:
        print("No running BDS or ProxyPass from this environment")

    if args.purge:
        if not (root / MARKER).exists():
            sys.exit(f"{root} has no {MARKER} marker; refusing to delete it")
        if args.keep_downloads:
            for entry in root.iterdir():
                if entry.name in ("downloads", MARKER):
                    continue
                remove_tree(entry) if entry.is_dir() else entry.unlink()
            print(f"Purged {root} (kept downloads)")
        else:
            remove_tree(root)
            print(f"Deleted {root}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, help_text):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--root", default=default_root(), help="environment directory (default: %(default)s)")
        sub.set_defaults(handler=handler)
        return sub

    setup = add("setup", cmd_setup, "download BDS, build ProxyPass and configure both")
    setup.add_argument("--version", default="1.26.63.1", help="BDS version (default: %(default)s)")
    setup.add_argument("--preview", action="store_true", help="use the preview BDS build")
    setup.add_argument("--proxy-port", type=int, default=19122)
    setup.add_argument("--server-port", type=int, default=19132)
    setup.add_argument("--transport", choices=("nethernet", "raknet"), default="nethernet")
    setup.add_argument("--proxypass-ref", default="master", help="ProxyPass branch, tag or commit")
    setup.add_argument("--java-home", help="JDK 17+ used to build and run ProxyPass")
    setup.add_argument("--rebuild", action="store_true", help="force a ProxyPass rebuild")

    start_bds = add("start-bds", cmd_start_bds, "run BDS in the foreground")
    start_bds.add_argument("--on-first-join", default="op @a",
                           help="console command sent when the first player spawns; '' to disable")

    add("start-proxypass", cmd_start_proxypass, "run ProxyPass in the foreground")

    teardown = add("teardown", cmd_teardown, "stop BDS and ProxyPass")
    teardown.add_argument("--purge", action="store_true", help="also delete the environment")
    teardown.add_argument("--keep-downloads", action="store_true", help="with --purge, keep downloaded zips")

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
