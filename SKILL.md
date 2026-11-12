---
name: bds-proxypass
description: Set up, start and tear down a vanilla Bedrock Dedicated Server behind ProxyPass to capture the packets real BDS sends. Use when asked to capture or compare vanilla Bedrock network behaviour, check what BDS sends for something, or to clean that environment up. The BDS version is a parameter. Works on Windows and Linux.
---

# BDS + ProxyPass capture environment

Runs a vanilla Bedrock Dedicated Server (BDS) with [ProxyPass](https://github.com/CloudburstMC/ProxyPass) in front of it, so a real client connects through the proxy and every packet is logged. Both sides run in offline mode over NetherNet by default.

```
Bedrock client  ->  ProxyPass 0.0.0.0:19122  ->  BDS 127.0.0.1:19132
```

Everything is one script, `scripts/bds_proxypass.py` next to this file, with four subcommands. It runs on Windows and Linux (the BDS build for the current OS is picked automatically) and needs Python 3.8+, git and a JDK; nothing else. ProxyPass currently builds with a JDK 17 toolchain, so JDK 17 itself must be installed (a newer JDK alone is not enough; setup says so if it is missing). Any JDK 17+ is used to run it. Use `python3` instead of `python` where that is the interpreter name.

Every subcommand takes `--root` (default `<temp dir>/bds-proxypass`, or `$BDS_PROXYPASS_ROOT` if set). Nothing is written to the repository.

## Arguments

The first argument, if given, is the BDS version (e.g. `/bds-proxypass 1.26.70.2`). Default: `1.26.63.1`. "teardown" or "purge" as the argument means skip to Teardown.

## Set up

Downloading BDS means accepting the Minecraft EULA and privacy policy; make sure the user asked for it.

```bash
python .claude/skills/bds-proxypass/scripts/bds_proxypass.py setup --version 1.26.63.1
```

| Option | Default | Purpose |
|---|---|---|
| `--version` | `1.26.63.1` | BDS version; fills `https://www.minecraft.net/bedrockdedicatedserver/bin-<win\|linux>/bedrock-server-<version>.zip` |
| `--preview` | off | Download from `bin-<os>-preview` instead |
| `--root` | `<temp dir>/bds-proxypass` | Where everything lives |
| `--proxy-port` / `--server-port` | `19122` / `19132` | Port the client joins / port BDS listens on |
| `--transport` | `nethernet` | `nethernet` or `raknet`, applied to both sides |
| `--proxypass-ref` | `master` | Branch, tag or commit of ProxyPass to build |
| `--java-home` | auto (prefers 21, needs 17+) | JDK used to run Gradle and ProxyPass |
| `--rebuild` | off | Force a ProxyPass rebuild |

Setup is idempotent: it reuses an existing download, extraction and jar. Each BDS version gets its own `bds-<os>-<version>` directory, so switching version is just re-running setup with a different `--version` (stop the running servers first).

What it configures. Both files are edited in place, key by key; comments and any other settings (including ones you changed by hand, such as `ignored-packets`) are kept across re-runs:

- BDS `server.properties`: `online-mode=false`, `allow-list=false`, `transport`, `server-port` and `server-udp-ports`, `enable-lan-visibility=false`, `gamemode=creative`, `allow-cheats=true`.
- ProxyPass `config.yml` (seeded from ProxyPass's own default): transport, proxy and destination host/port, `verify-client-authentication: false`, `log-packets: true`, `log-to: file`.

**Check the codec.** ProxyPass hard-codes one protocol codec. Setup prints it (`codec Bedrock_vNNNN`) and stores it in `env.json`. If it does not match the protocol of the BDS version, the client will fail to join; pick a `--proxypass-ref` that matches, or a different BDS version.

**Linux:** BDS is built for Ubuntu and needs its usual shared libraries (e.g. `libcurl4`). If `bedrock_server` fails to start with a missing-library error, tell the user which package is missing rather than installing it yourself.

## Start

Run each in its own terminal tab (use `run_in_terminal` where available, not a background shell, so the user can type into the BDS console). Start BDS first.

```bash
python .claude/skills/bds-proxypass/scripts/bds_proxypass.py start-bds
```

```bash
python .claude/skills/bds-proxypass/scripts/bds_proxypass.py start-proxypass
```

Ready when BDS prints `Server started.` and ProxyPass prints `NETHERNET listener started on /0.0.0.0:19122` (or `RAKNET ...`).

`start-bds` wraps the BDS console: when the first player spawns it sends `op @a` so the tester has operator permissions (offline players have no XUID, so this is not persisted and runs once per server start). Change or disable it with `--on-first-join '<command>'` / `--on-first-join ''`. Lines typed into the tab are passed through to BDS.

The user then joins from a Bedrock client: add a server with this machine's LAN IP (or `127.0.0.1`) and port `19122` (the proxy, not BDS on `19132`). If a firewall prompts for `bedrock_server` or Java, the user has to allow it themselves.

## Capture

- Packet logs: `<root>/proxypass-run/sessions/<player>-<timestamp>/packets.log`, one folder per client session.
- Dumped registries (block palette, items, etc.): `<root>/proxypass-run/data/`.
- Noisy packets are excluded via `ignored-packets` in `<root>/proxypass-run/config.yml`; edit and restart ProxyPass to change them.
- BDS console commands have no leading slash, e.g. `summon enderman`, `gamerule domobspawning false`.

Logs reach tens of MB within a minute and individual lines are very long. Search with Grep (use `-o` to print only the match) for the packet or field of interest rather than reading the file. Block IDs appear as hashed runtime IDs because ProxyPass has no block palette loaded.

## Teardown

Stop the servers, keeping files for next time. This kills only the processes the start commands recorded in `<root>/run/*.pid`, and only if they are still BDS or Java:

```bash
python .claude/skills/bds-proxypass/scripts/bds_proxypass.py teardown
```

Delete everything, or everything except the downloaded zips:

```bash
python .claude/skills/bds-proxypass/scripts/bds_proxypass.py teardown --purge
```

```bash
python .claude/skills/bds-proxypass/scripts/bds_proxypass.py teardown --purge --keep-downloads
```

`--purge` deletes the world and all captured packet logs, so copy anything worth keeping out of `sessions/` first and confirm with the user before running it. It refuses to delete a directory that lacks the `.bds-proxypass-env` marker setup creates.

Also close any terminal tabs opened for the servers.
