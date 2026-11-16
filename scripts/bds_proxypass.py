#!/usr/bin/env python3
"""BDS + ProxyPass capture environment for Windows and Linux. Standard library only."""
import argparse
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

WINDOWS = os.name == "nt"
EXE_SUFFIX = ".exe" if WINDOWS else ""
MARKER = ".bds-proxypass-env"
DOWNLOADS = "downloads"

# Each server is tracked under its name and, before teardown kills that pid, checked against the
# executable here, so a stale pid file cannot take an unrelated process with it.
BDS, PROXYPASS = "bds", "proxypass"
EXECUTABLE = {BDS: "bedrock_server", PROXYPASS: "java"}
BDS_EXE = EXECUTABLE[BDS] + EXE_SUFFIX

DOWNLOAD_BASE = "https://www.minecraft.net/bedrockdedicatedserver"
MAIN_CLASS = "org/cloudburstmc/proxypass/ProxyPass.class"
PROXYPASS_API = "https://api.github.com/repos/CloudburstMC/ProxyPass/releases/latest"
PROXYPASS_JAR = "https://github.com/CloudburstMC/ProxyPass/releases/download/{build}/ProxyPass.jar"
# minecraft.net rejects requests without a browser-like user agent.
USER_AGENT = "Mozilla/5.0"


def default_root():
    return os.environ.get("BDS_PROXYPASS_ROOT") or str(Path(tempfile.gettempdir()) / "bds-proxypass")


def root_of(args):
    return Path(args.root).resolve()


def load_env(root):
    env_file = root / "env.json"
    if not env_file.exists():
        sys.exit(f"No env.json in {root}; run setup first")
    return json.loads(env_file.read_text())


def java_major(home):
    release = Path(home) / "release"
    if release.exists():
        match = re.search(r'JAVA_VERSION="(\d+)', release.read_text(errors="ignore"))
        if match:
            return int(match.group(1))
    return 0


def java_bin(home):
    return Path(home) / "bin" / (EXECUTABLE[PROXYPASS] + EXE_SUFFIX)


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
    on_path = shutil.which(EXECUTABLE[PROXYPASS])
    if on_path:
        candidates.append(str(Path(on_path).resolve().parent.parent))

    jdks = {}
    for home in candidates:
        if home and java_bin(home).exists() and java_major(home):
            jdks.setdefault(str(Path(home).resolve()), java_major(home))
    return jdks


def pick_runtime_jdk(jdks, minimum):
    usable = [home for home in jdks if jdks[home] >= minimum]
    return sorted(usable, key=lambda home: (jdks[home] != minimum, -jdks[home]))[0] if usable else None


def download(url, dest):
    part = dest.with_name(dest.name + ".part")
    print(f"Downloading {url}")
    curl = shutil.which("curl")
    if curl:
        result = subprocess.run([curl, "-sS", "-L", "--fail", "-A", USER_AGENT, "-o", str(part), url])
        if result.returncode != 0:
            sys.exit(f"Download failed (exit {result.returncode}): {url}")
    else:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request) as response, open(part, "wb") as out:
            shutil.copyfileobj(response, out)
    part.replace(dest)


def latest_proxypass_build():
    """Tag of the newest ProxyPass release, e.g. '1'."""
    request = urllib.request.Request(PROXYPASS_API, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)["tag_name"]
    except Exception as error:
        sys.exit(f"Could not look up the latest ProxyPass release ({error}); pass --proxypass-build")


def jar_resource(jar, name):
    with zipfile.ZipFile(jar) as archive:
        return archive.read(name)


def jar_built_against(jar):
    """The protocol codec and the Java feature version this build was compiled with."""
    data = jar_resource(jar, MAIN_CLASS)
    codec = re.search(rb"Bedrock_v(\d+)", data)
    class_file_version = struct.unpack(">H", data[6:8])[0]
    return (f"Bedrock_v{codec.group(1).decode()}" if codec else "unknown", class_file_version - 44)


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


def setup_bds(args, root, downloads):
    """Downloads, extracts and configures BDS. Returns its share of env.json."""
    platform = "win" if WINDOWS else "linux"
    bds_dir = root / f"bds-{platform}-{args.version}"
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
        "transport": "nethernet",
        "server-port": args.server_port,
        "server-portv6": args.server_port + 1,
        "server-udp-ports": args.server_port,
        "enable-lan-visibility": "false",
        "gamemode": "creative",
        "allow-cheats": "true",
        "level-name": "proxypass-test",
    })
    return {"version": args.version, "preview": args.preview, "serverPort": args.server_port,
            "bdsDir": str(bds_dir)}


def setup_proxypass(args, downloads, run_dir):
    """Downloads the jar, picks a JDK that can run it and writes its config. Returns its share of env.json."""
    build = args.proxypass_build if args.proxypass_build != "latest" else latest_proxypass_build()
    jar = downloads / f"ProxyPass-{build}.jar"
    if not jar.exists():
        download(PROXYPASS_JAR.format(build=build), jar)
    codec, needed = jar_built_against(jar)

    jdks = find_jdks(args.java_home)
    java_home = str(Path(args.java_home).resolve()) if args.java_home else pick_runtime_jdk(jdks, needed)
    if not java_home or jdks.get(java_home, 0) < needed:
        sys.exit(f"ProxyPass build {build} needs JDK {needed}+ and none was found; install it or pass --java-home")
    print(f"Using JDK {jdks[java_home]} at {java_home}")

    # ProxyPass ships its defaults inside the jar, so seed from those and change only what this
    # environment needs; the rest of the file is the user's to edit.
    config = run_dir / "config.yml"
    if not config.exists():
        config.write_bytes(jar_resource(jar, "config.yml"))
    patch_yaml(config, [
        ("proxy", "host", "0.0.0.0"),
        ("proxy", "port", args.proxy_port),
        ("destination", "host", "127.0.0.1"),
        ("destination", "port", args.server_port),
        ("nethernet", "verify-client-authentication", "false"),
        (None, "log-packets", "true"),
        (None, "log-to", "file"),
    ])
    return {"proxyPort": args.proxy_port, "proxyRunDir": str(run_dir), "proxyJar": str(jar),
            "javaHome": java_home, "javaVersion": needed, "proxyBuild": build, "proxyCodec": codec}


def cmd_setup(args):
    if not re.fullmatch(r"\d+(\.\d+){2,3}", args.version):
        sys.exit(f"Version '{args.version}' does not look like a BDS version (e.g. 1.26.63.1)")
    if args.proxy_port == args.server_port:
        sys.exit("--proxy-port and --server-port must differ")

    root = root_of(args)
    downloads = root / DOWNLOADS
    run_dir = root / "proxypass-run"
    for directory in (downloads, run_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("Created by the bds-proxypass skill. Safe to delete.\n")

    env = {**setup_bds(args, root, downloads), **setup_proxypass(args, downloads, run_dir)}
    (root / "env.json").write_text(json.dumps(env, indent=2) + "\n")

    print()
    print(f"Environment ready in {root}")
    print(f"  BDS {env['version']} -> 127.0.0.1:{env['serverPort']} (nethernet, offline)")
    print(f"  ProxyPass build {env['proxyBuild']} -> 0.0.0.0:{env['proxyPort']} (codec {env['proxyCodec']})")
    print(f"  Packet logs -> {run_dir / 'sessions'}")


def pid_file(root, name):
    return root / "run" / f"{name}.pid"


def supervise(root, name, process, watch=None):
    """Records the pid while the process runs, then makes sure it is gone and the record with it."""
    path = pid_file(root, name)
    path.parent.mkdir(exist_ok=True)
    path.write_text(str(process.pid))
    try:
        watch() if watch else process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        path.unlink(missing_ok=True)


def cmd_start_bds(args):
    root = root_of(args)
    bds_dir = Path(load_env(root)["bdsDir"])
    child_env = dict(os.environ)
    if not WINDOWS:
        # BDS loads the shared libraries shipped beside it.
        child_env["LD_LIBRARY_PATH"] = str(bds_dir)
    process = subprocess.Popen([str(bds_dir / BDS_EXE)], cwd=bds_dir, env=child_env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", bufsize=1)

    def send(command):
        try:
            process.stdin.write(command.rstrip("\r\n") + "\n")
            process.stdin.flush()
        except (OSError, ValueError):
            pass

    def forward_console():
        for line in sys.stdin:
            send(line)

    def relay_output():
        sent = not args.on_first_join
        for line in process.stdout:
            print(line, end="", flush=True)
            if not sent and "Player Spawned" in line:
                print(f"[start-bds] > {args.on_first_join}", flush=True)
                send(args.on_first_join)
                sent = True

    threading.Thread(target=forward_console, daemon=True).start()
    supervise(root, BDS, process, relay_output)


def cmd_start_proxypass(args):
    root = root_of(args)
    cfg = load_env(root)
    # ProxyPass reads config.yml and writes sessions/ relative to the working directory.
    process = subprocess.Popen([str(java_bin(cfg["javaHome"])), "--enable-native-access=ALL-UNNAMED",
                                "-jar", cfg["proxyJar"]], cwd=cfg["proxyRunDir"])
    supervise(root, PROXYPASS, process)


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
    def make_writable(function, target, _):
        os.chmod(target, 0o700)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


def stop_servers(root):
    stopped = False
    for name, executable in EXECUTABLE.items():
        path = pid_file(root, name)
        if not path.exists():
            continue
        try:
            pid = int(path.read_text().strip())
        except ValueError:
            pid = None
        running = process_name(pid) if pid else None
        if running and running.startswith(executable):
            print(f"Stopping {name} (pid {pid})")
            kill(pid)
            stopped = True
        path.unlink(missing_ok=True)
    if not stopped:
        print("No running BDS or ProxyPass from this environment")


def purge(root, keep_downloads):
    if not (root / MARKER).exists():
        sys.exit(f"{root} has no {MARKER} marker; refusing to delete it")
    if keep_downloads:
        for entry in root.iterdir():
            if entry.name in (DOWNLOADS, MARKER):
                continue
            remove_tree(entry) if entry.is_dir() else entry.unlink()
        print(f"Purged {root} (kept downloads)")
    else:
        remove_tree(root)
        print(f"Deleted {root}")


def cmd_teardown(args):
    root = root_of(args)
    if not root.exists():
        print(f"Nothing at {root}")
        return
    stop_servers(root)
    if args.purge:
        purge(root, args.keep_downloads)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, help_text):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--root", default=default_root(), help="environment directory (default: %(default)s)")
        sub.set_defaults(handler=handler)
        return sub

    setup = add("setup", cmd_setup, "download BDS and ProxyPass and configure both")
    setup.add_argument("--version", default="1.26.63.1", help="BDS version (default: %(default)s)")
    setup.add_argument("--preview", action="store_true", help="use the preview BDS build")
    setup.add_argument("--proxy-port", type=int, default=19122)
    setup.add_argument("--server-port", type=int, default=19132)
    setup.add_argument("--proxypass-build", default="latest", help="ProxyPass release to download (default: %(default)s)")
    setup.add_argument("--java-home", help="JDK used to run ProxyPass; must match the release")

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
