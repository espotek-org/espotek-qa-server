# espotek-qa-server

Automatic release QA for the **EspoTek Labrador Unified App** (`Unified_App/` in
`espotek-org/labrador`, released under the rolling `unified-continuous` tag).
The old Qt app (`Desktop_Interface/`) is out of scope.

The goal is a repo you can clone onto a spare MacBook, run `./bootstrap.sh`, and
end up with a new QA tester: a machine with a real Labrador board attached that
notices each new release, installs it in fresh macOS / Linux / Windows
environments, runs the app's built-in hardware and GUI QA against the installed
binaries, and publishes the results to `https://qa-results.espotek.com` behind a
password, where a human or an agent can inspect them.

This repo is **public**. Never commit secrets, host-specific addresses, account
passwords or tunnel credentials; those live in the host's local config
(`/Users/Shared/espotek-qa/config.toml` and the `qaweb` account's home), which
`bootstrap.sh` creates.

## HARD CONSTRAINT: waveform tests MUST run at exactly 831 Hz (owner, 2026-11-28)

This is not a guideline, and it is not up for reinterpretation when tests are
added, rewritten or refactored. It was acknowledged once and then broken by
the next batch of new tests; that must not happen again.

- The `--auto-qa` suite MUST contain a battery of waveform tests driven at
  **exactly 831 Hz** (`831.0`, not "about 800 Hz", not 830 or 833). At
  831 Hz the battery covers every generator wave shape (sine, square,
  triangle, sawtooth) on both SG1→CH1 and SG2→CH2 (frequency, levels and
  shape), the scope gains, the triggers, and the per-transport measure
  tests. When a new waveform test is added, an 831 Hz version of it is added
  with it.
- "Normal" frequencies (500 Hz, 1 kHz, 2 kHz, 4 kHz, 10 kHz, and any other
  whose period divides evenly into the 1 ms USB frame / 375-sample frame)
  MASK dropped and misaligned packets. The waveform repeats exactly every
  frame, so a lost frame that librador back-fills with the previous one, or
  an endpoint a frame out of step, looks identical to a clean capture and
  the test passes (FINDINGS.md: "only an odd frequency shows it", "Lost
  frames make every signal read as 1 kHz"). At 831 Hz the period is
  1.2034 ms (451.26 samples at 375 kS/s), so a dropped, repeated or shifted
  frame shows up as a kink, a step, a wrong pulse width or a wrong cycle
  count.
- Tests at other frequencies (sweeps, 2 kHz baselines, the 354.1 / 1234.5 Hz
  soak, the 891 / 557 Hz screenshot tour) are allowed in addition. They never
  replace the 831 Hz battery or count towards it.
- Before reporting any change to the suite as done, check that the 831 Hz
  battery is still present and runs on every target, and say so in the
  report. A suite without it is unfinished, not "done with a caveat".

## How a run works

1. `qa poll` (launchd, every 15 min) asks the GitHub API for the
   `unified-continuous` release. When its target commit or asset hashes
   differ from the last tested release, it starts `qa run`.
2. `qa run` downloads the release assets (sha256-verified against GitHub's
   digests) and tests each target in turn, holding the board lock
   (`/Users/Shared/labrador-board.lock`, an exclusive `flock(2)`; macOS has no
   `lockf(1)` command, so shell users need a small flock wrapper, and on macOS
   flock and fcntl locks exclude each other), because only one consumer can own the board. Targets are
   `linux-deb`, `linux-appimage`, `windows` and `macos`. Each runs in a fresh
   *session* (`session.py`):
   - Linux and Windows: a throwaway qcow2 overlay on the platform's golden
     image, booted with the Labrador passed through (`usb-host` for 03eb:ba94,
     03eb:2fe4 and 03eb:a000, so the board follows itself through the DFU
     bootloader during firmware flashing)
   - macOS: natively on the host, in the dedicated standard account `qamac`
     (the auto-login console user), whose copy of the app and its data are
     wiped before and after every run
3. In the session the server installs the asset the way a user would and runs
   its own install checks (installer exit code, files, drivers, udev rule and
   permissions, the .deb's dependency closure, codesign/notarization), then
   runs the installed app's QA: `labrador --auto-qa <out_dir>` (on Windows
   through the desktop-session job runner). It follows `<out_dir>/progress.log`
   and takes host-side screendumps of the VM display every 20 s, so OS-level
   dialogs that block a run show up in the results.
4. The install checks and the app's `report.json` are merged into
   `results/<run-id>/<target>/report.json`; `run.json` and `index.json` feed
   the results site.

## The --auto-qa contract (implemented in espotek-org/labrador)

The QA lives in the app itself, so every release tests itself with its own
code: a single argument on the main executable, `labrador --auto-qa <out_dir>`
(project owner's requirement). It is being built on branch `qa-suite` of the
labrador repo. The server depends only on this:

- exit 0 iff every test passed
- `<out_dir>/progress.log` appears within seconds and gains flushed progress
  lines. Builds from before `--auto-qa` ignore unknown arguments and just open
  their window, so no `progress.log` within 120 s is recorded as "this build
  doesn't support --auto-qa" and the app is stopped
- `<out_dir>/report.json`, `schema_version: 1`: `suite`, `app`, `platform`,
  `started_at`, `finished_at`, `result` (`pass|fail`), `counts`, `tests[]`
  (`id, group, title, status, duration_s, message, measurements[], artifacts[]`)
  and `screenshots[]` (`id, path, caption, expect`), paths relative to `<out_dir>`
- it never touches the user's real settings, never calibrates, and leaves the
  board on the bundled firmware

## Facts established (2026-11-23)

### The QA host ("air")
- 2018/2019 Intel MacBook Air: i5-8210Y (2 cores / 4 threads), 8 GB RAM,
  ~216 GB free, macOS 14.7 Sonoma (its last supported macOS). VT-x and
  Hypervisor.framework are available (`kern.hv_support=1`).
- Run **one VM at a time**. The host idles at about 4 GB used, so guests get
  ≤ 4 GB.
- The Labrador is attached (03eb:ba94, full speed). Its signal generator
  outputs are looped back to the scope inputs (believed SG1→CH1, SG2→CH2; the
  app's --auto-qa measures and records the real wiring).
- **Homebrew refuses Intel Macs** ("Homebrew on macOS is only supported on
  Apple Silicon processors!"), so this project uses **MacPorts** for qemu,
  cloudflared, cmake, ninja, libusb and pkgconfig. MacPorts also works on Apple
  Silicon, so there is one package manager on every host.
- The runner works from the admin account that ran `bootstrap.sh`. Public-facing
  services (results web app, cloudflared) run as a separate unprivileged
  account, `qaweb`.

- The board sits behind a VIA VL820 USB 2.0 hub in a USB-C dock (with a
  Realtek Ethernet adapter). Full-speed bulk through it delivers exactly half
  the 1 ms frames (a 60 s soak got 29991 of 60000), which fails the stream
  and low-frequency tests. Plugging the board straight into the Air (a
  USB-C to micro-USB cable, no hub) is the expected fix; until that's
  verified, treat stream-integrity failures on this host as suspect.

### Hypervisor and platform decisions
- **QEMU with HVF** (owner's choice) over VMware Fusion (Fusion needs a
  Broadcom login to download; Homebrew dropped its cask and VMware's public
  CDS returns 404). MacPorts builds QEMU from source on Intel (about an hour
  on the i5-8210Y).
- **macOS runs natively** in the `qamac` account, not in a VM (the owner
  allowed this). A macOS guest would need half of the 8 GB, relies on
  OpenCore under Intel HVF, and would still be capped at Sonoma (the newest
  macOS this MacBook can install), so it would add risk without adding an OS
  version. Native runs also exercise the real GPU and USB stack. The cost is
  that there are no host-side display captures for macOS (the app's own frame
  dumps still work).
- `qamac` is the auto-login console user, so the tester stays reachable over
  Tailscale through a launch agent that opens Tailscale.app in qamac's
  session. The standalone app doesn't run before login, but its tailnet
  state is system-wide (`/Library/Tailscale`), so the same node comes up
  (verified by a reboot on 2026-11-23: back on the tailnet 49 s after boot).
  Keep a LAN route to the tester when changing anything about login or
  Tailscale. qamac's shell is bash, like the Linux guest's, because the
  runner's guest commands are bash.
- A GUI app started over ssh as qamac draws in qamac's console session:
  `labrador --auto-qa` from the `qa-suite` branch ran that way with all 36
  screenshots on 2026-11-23.
- **Windows 11 runs unactivated** (owner's decision). Microsoft's software-
  download API refuses automated requests from this network ("Sentinel marked
  this request as rejected"), so the ISO is downloaded by hand in a browser
  and handed over with `qa vm build windows --iso <file>`. Don't try to work
  around that refusal. The generic KMS client key in `autounattend.xml` only
  selects the Pro edition. TPM, Secure Boot and RAM checks are bypassed.
- Windows GUI work runs inside the auto-logged-on desktop through two
  scheduled tasks (`QA-User`, non-elevated, for `--auto-qa`; `QA-Elevated` for
  the installer), because processes started over ssh have no visible desktop.
  The VM has no GL 3 driver, so Mesa's llvmpipe `opengl32.dll` is copied next
  to `labrador.exe` at install time (recorded as an `install.env.*` check).

### The Unified App and its releases
- `unified-continuous` assets: `Labrador-for-macOS.dmg` (signed + notarized,
  universal), `Labrador-for-Windows.exe` (Inno installer; `/VERYSILENT
  /SUPPRESSMSGBOXES /NORESTART`; installs WinUSB/libusb driver packages via
  dpinst), `Labrador-for-Linux-x86_64.AppImage` and `.deb` (+ arm64 and an
  Android APK, not tested here).
- Release builds are plain builds. The ImGui Test Engine suite (`--qa`) only
  exists in `LABRADOR_QA=ON` builds, so the release QA path can't use it.
  Plain builds support `--smoke`, `LABRADOR_FRAME_DUMP`, `LABRADOR_LAYOUT`,
  `LABRADOR_WINDOW_SIZE` and `LABRADOR_NO_USB`.
- `labrador-cli` ships in every package. As released on 2026-10-22 it only has
  `capture` and `update-firmware`. The QA doesn't go through it: `--auto-qa`
  drives the board in-process through librador.
- As released on 2026-10-22 the app ignores unknown arguments (`src/main.cpp`),
  which is why the server needs the progress.log startup probe.
- The app needs desktop OpenGL 3.2 (`#version 150`). VMs without 3D render
  through software GL (Mesa llvmpipe on Linux).
- USB IDs: 03eb:ba94 app firmware, 03eb:2fe4 Atmel DFU bootloader, 03eb:a000
  legacy. Transports: iso6 (Windows/Linux default), iso1, bulk (macOS default).

### Services on the tester
- `com.espotek.qa.poll` (launchd, admin account, every 15 min): `qa poll`
- `com.espotek.qa.web` (account `qaweb`): the results site on 127.0.0.1:8890
- `com.espotek.qa.tunnel` (account `qaweb`): cloudflared tunnel
  `espotek-qa-results` → `qa-results.espotek.com`
- Secrets on the host: `/Users/Shared/espotek-qa/secrets/` (results-site
  password, `qamac` password; mode 600, admin only),
  `~qaweb/.config/espotek-qa/web.env` (password hash, session key) and
  `~qaweb/.cloudflared/<tunnel-id>.json` (tunnel credentials).

### Findings so far
- 2026-11-23, release c9a578a: the `.deb` declares `Depends: libgl1` but
  `/usr/bin/labrador` links `libOpenGL.so.0` from `libopengl0`, which nothing
  in its dependency closure provides, so on systems without `libopengl0` the
  GUI fails to start (`install.dependency_closure`). The Linux golden image
  includes `libopengl0` like any desktop, so the AppImage (which rightly
  relies on the host for GLVND) can be tested; the closure check catches the
  .deb's gap regardless of what the image contains.

### Testing the server itself
`uv run pytest` covers the results site (auth, throttling, path safety, the
env-file parsing) and the report merge. End-to-end behaviour is only proven
by a real run on the tester.

## Conventions
- Python via **uv** only (`uv run`, `uv add`, `uv sync`), never pip or python
  directly.
- Never suppress errors. Config keys are required (`cfg["key"]`), missing
  fields raise, and lookups raise instead of returning sentinels.
- One path, no fallbacks: no "if new thing configured, else old way" branches.
- Long-running commands print progress (phase, counts, elapsed).
- Default branch is `master`.
