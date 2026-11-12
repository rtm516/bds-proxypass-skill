# bds-proxypass

A [Claude Code](https://claude.com/claude-code) skill that runs a vanilla
[Bedrock Dedicated Server](https://www.minecraft.net/en-us/download/server/bedrock) (BDS) behind
[ProxyPass](https://github.com/CloudburstMC/ProxyPass), so a real client connects through the proxy
and every packet it exchanges with the server is logged. Use it to see what vanilla BDS actually
sends, as a reference to compare a custom server against.

```
Bedrock client  ->  ProxyPass 0.0.0.0:19122  ->  BDS 127.0.0.1:19132
```

Both sides run offline over NetherNet by default. The BDS version is a parameter, so several
versions can sit side by side.

## Requirements

- **Windows or Linux** — the matching BDS build is picked automatically.
- **Python 3.8+** — standard library only, nothing to install.
- **Git**, on `PATH`, to clone ProxyPass.
- **A JDK**. ProxyPass pins a JDK 17 Gradle toolchain, so JDK 17 itself has to be installed to build
  it; a newer JDK alone is not enough. Any JDK 17+ is used to run the built jar, preferring 21.
- **A Bedrock client** to join with.

Downloading BDS means accepting the [Minecraft EULA](https://minecraft.net/terms) and
[privacy policy](https://go.microsoft.com/fwlink/?LinkId=521839).

## Install

Unpack the zip from the [latest release](../../releases/latest) into your skills folder, or clone
this repository straight into it:

```bash
git clone https://github.com/rtm516/bds-proxypass-skill ~/.claude/skills/bds-proxypass
```

Use `.claude/skills/bds-proxypass` inside a project to scope it to that project. Then ask Claude to
capture Bedrock packets, or invoke it directly:

```
/bds-proxypass 1.26.63.1
```

The script has no dependency on the skill wrapper, so it can also be run directly.

## Usage

```bash
python scripts/bds_proxypass.py setup --version 1.26.63.1
```

Then start each server in its own terminal, BDS first:

```bash
python scripts/bds_proxypass.py start-bds
```

```bash
python scripts/bds_proxypass.py start-proxypass
```

They are ready when BDS prints `Server started.` and ProxyPass prints
`NETHERNET listener started on /0.0.0.0:19122`. In the Bedrock client, add a server pointing at this
machine (`127.0.0.1`, or the LAN IP from another device) on port **19122** — the proxy, not BDS on
19132. The first player to spawn is opped automatically.

`start-bds` passes anything you type through to the BDS console, without the leading slash:
`summon enderman`, `gamerule domobspawning false`.

Stop both servers with `teardown`, keeping the files for next time, or `teardown --purge` to delete
the environment (optionally `--keep-downloads`). Purging deletes the world and every captured log,
so copy anything worth keeping out of `sessions/` first.

```bash
python scripts/bds_proxypass.py teardown
```

### setup options

| Option | Default | Purpose |
|---|---|---|
| `--version` | `1.26.63.1` | BDS version to download |
| `--preview` | off | Use the preview channel |
| `--root` | `<temp dir>/bds-proxypass` | Where the environment lives; `$BDS_PROXYPASS_ROOT` also sets it |
| `--proxy-port` / `--server-port` | `19122` / `19132` | Port the client joins / port BDS listens on |
| `--transport` | `nethernet` | `nethernet` or `raknet`, applied to both sides |
| `--proxypass-ref` | `master` | ProxyPass branch, tag or commit to build |
| `--java-home` | auto | JDK used to build and run ProxyPass |
| `--rebuild` | off | Force a ProxyPass rebuild |

Setup is idempotent: an existing download, extraction and jar are reused, and `server.properties`
and `config.yml` are patched key by key, so comments and your own edits survive a re-run. Nothing is
written into this repository.

## Reading the captures

Packet logs land in `<root>/proxypass-run/sessions/<player>-<timestamp>/packets.log`. They grow to
tens of megabytes within a minute and individual lines are very long, so grep for the packet or
field you care about (`grep -o` to print just the match) rather than opening the file.

Block IDs appear as hashed runtime IDs, because ProxyPass has no block palette loaded; the registries
dumped to `<root>/proxypass-run/data/` resolve them. To quieten the log, edit `ignored-packets` in
`<root>/proxypass-run/config.yml` and restart ProxyPass.

## Gotchas

- **Codec mismatch.** ProxyPass hard-codes one protocol codec. Setup prints it (`codec Bedrock_vNNNN`).
  If it does not match the protocol of the BDS version you picked, the client cannot join — choose a
  `--proxypass-ref` that matches, or a different `--version`.
- **Switching versions.** Stop the servers, then re-run `setup` with a different `--version`. Each
  version gets its own directory, so nothing is clobbered.
- **Ports.** Joining 19132 bypasses the proxy and captures nothing. Use 19122.
- **Linux shared libraries.** BDS is built for Ubuntu and needs its usual libraries (e.g. `libcurl4`).
- **Firewall.** Windows may prompt for `bedrock_server` or Java the first time.

## Credits

[ProxyPass](https://github.com/CloudburstMC/ProxyPass) is by CloudburstMC. Minecraft and the Bedrock
Dedicated Server are Mojang/Microsoft products, downloaded from minecraft.net under their own terms;
this repository ships neither.
