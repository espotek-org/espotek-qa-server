# espotek-qa-server

Automatic release QA for the [EspoTek Labrador](https://espotek.com/labrador)
Unified App. A spare MacBook with a Labrador board attached (signal generator
looped back into the scope) watches for new releases of
[espotek-org/labrador](https://github.com/espotek-org/labrador), installs each
one the way a user would on Linux (.deb and AppImage), Windows and macOS, runs
the installed app's own QA (`labrador --auto-qa <dir>`: hardware loopback and
GUI tests), and publishes the results, screenshots included, to a
password-protected site.

How it works and why it's built this way: [CLAUDE.md](CLAUDE.md).

## Turning a MacBook into a QA tester

You need an admin account that can sudo, the Labrador plugged in (SG1 looped
to CH1, SG2 to CH2), and the Cloudflare tunnel's credentials file.

```sh
git clone https://github.com/espotek-org/espotek-qa-server.git
cd espotek-qa-server
./bootstrap.sh --tunnel-credentials <tunnel-id>.json
```

What it does, in order (rerunning it is safe and is how you deploy changes):

1. Turns sleep off and enables restart after power loss.
2. Installs the Xcode Command Line Tools, then MacPorts (Homebrew no longer
   installs on Intel Macs), then `qemu` and `cloudflared` from MacPorts. On
   an Intel Mac QEMU compiles from source, which takes about an hour.
3. Installs uv (for this server's own Python), copies this checkout to `/Users/Shared/espotek-qa/app`, and
   links `qa` into `/usr/local/bin`.
4. Creates the SSH key the runner uses for its guests, and `config.toml`.
5. Creates `qaweb`, an unprivileged account that runs the public-facing
   pieces: the results site and the Cloudflare tunnel.
6. Creates `qamac`, the standard account the macOS target runs in, and makes
   it the auto-login user (reboot once afterwards).
7. Installs three launchd services: the results site, the tunnel, and the
   15-minute release poll.
8. Builds the Linux golden VM image. The Windows image needs an ISO that
   Microsoft won't serve to scripts, so see below.

The results site's password is printed at the end of the first run and kept in
`/Users/Shared/espotek-qa/secrets/web-password`.

### The Windows image

Microsoft refuses automated ISO downloads, so get the ISO in a browser from
<https://www.microsoft.com/software-download/windows11> (Windows 11
multi-edition ISO for x64, English) and run:

```sh
qa vm build windows --iso ~/Downloads/Win11_*.iso
```

The unattended install takes 30 to 90 minutes. Windows runs unactivated.

### The Cloudflare tunnel

The first tester needs a tunnel, created once from any machine where
`cloudflared tunnel login` has been run for the espotek.com zone:

```sh
cloudflared tunnel create espotek-qa-results
cloudflared tunnel route dns espotek-qa-results qa-results.espotek.com
# then pass ~/.cloudflared/<tunnel-id>.json to bootstrap.sh
```

To move the tester to another MacBook, copy
`/Users/qaweb/.cloudflared/<tunnel-id>.json` from the old one and pass it to
`bootstrap.sh` on the new one. The hostname follows the tunnel.

## Day-to-day commands (on the tester)

```sh
qa run                        # test the current unified-continuous release now
qa run --targets linux-deb    # just one target (linux-deb, linux-appimage, windows, macos)
qa poll                       # what launchd runs: test the release if it changed
qa vm build linux --force     # rebuild a golden image (linux, windows)
qa vm shell linux             # fresh throwaway session with the board attached, and a shell in it
```

## Results

<https://qa-results.espotek.com>, behind the password. Each run lists its
targets; each target shows the install checks and the app's tests (failures
first, with measurements against their limits), the app screenshots with what
each should show, and captures of the whole VM display taken every 20 seconds.
Agents should read `/agents` there, which describes the JSON API (HTTP Basic
auth with the same password).

## The tests

The tests are built into the app (`labrador --auto-qa <dir>`), so every
release is tested by its own code. This server adds the install checks and
relies only on the contract described in [CLAUDE.md](CLAUDE.md). Releases
from before `--auto-qa` still get the install checks, and their runs say the
build doesn't support `--auto-qa`.
