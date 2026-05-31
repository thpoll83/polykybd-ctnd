# PolyKybd CTND

Continuous Test and Deploy station for the [PolyKybd](https://github.com/thpoll83/qmk_firmware) split keyboard.
Runs on a Raspberry Pi 4 with a 7" touchscreen and provides automated flashing, HID console reading, and GitHub Actions HIL (hardware-in-the-loop) CI integration — no physical button pressing required.

## Features

- Automated UF2 flashing of both keyboard halves via USB (GPIO-controlled BOOTSEL + uhubctl power switching)
- QMK HID console log streaming to the UI
- Raw HID command interface (same protocol as PolyKybdHost)
- Touch-friendly web UI optimised for 1024×600 (Flask + SocketIO, dark theme)
- GitHub Actions self-hosted runner integration (`runs-on: [self-hosted, polykybd-ctnd]`)

## Hardware Bill of Materials

| Item | Qty | Notes |
|---|---|---|
| Raspberry Pi 4 | 1 | Any RAM variant |
| 52Pi 7" 1024×600 capacitive touch display | 1 | HDMI + USB, no driver needed |
| PolyKybd PCB halves (assembled) | 2 | Keys and screen not required |
| USB-A to USB-C cable | 2 | One per half |
| Signal wire | 6 | GPIO → PCB pads |
| 100 Ω resistor | 4 | Series protection on each GPIO line |
| Split cable | 1 | TRRS or equivalent — stays permanently connected |

---

## GPIO Wiring

The RPi4 drives the **RUN** (reset) and **BOOTSEL** pins on each RP2040 PCB.
Both signals are active-low; the RP2040 has internal pull-ups so no external pull-ups are needed.
The 100 Ω series resistors protect against any momentary voltage conflicts.

### RPi4 40-pin header — relevant pins

```
RPi4 header                          signal
─────────────────────────────────────────────
 (6) GND  ──────────────────────────► Left half  GND pad
(11) GPIO17 ──[100Ω]────────────────► Left half  RUN pad
(12) GPIO18 ──[100Ω]────────────────► Left half  BOOTSEL pad

(14) GND  ──────────────────────────► Right half GND pad
(15) GPIO22 ──[100Ω]────────────────► Right half RUN pad
(16) GPIO23 ──[100Ω]────────────────► Right half BOOTSEL pad
```

Pins 11–16 are a tight cluster on the header — all six connections (4 signals + 2 GND) sit within six adjacent physical pins, making for clean point-to-point wiring.

### Full 40-pin header reference

`●` = connected / used for this project  `○` = unused

```
    3.3V ( 1) ○ ○ ( 2) 5V
   GPIO2 ( 3) ○ ○ ( 4) 5V
   GPIO3 ( 5) ○ ● ( 6) GND         ◀── Left GND
   GPIO4 ( 7) ○ ○ ( 8) GPIO14
     GND ( 9) ○ ○ (10) GPIO15
  GPIO17 (11) ● ○ (12) GPIO18       ◀── Left  RUN(11) / BOOTSEL(12)
  GPIO27 (13) ○ ● (14) GND          ◀── Right GND
  GPIO22 (15) ● ● (16) GPIO23       ◀── Right RUN(15) / BOOTSEL(16)
   3.3V  (17) ○ ○ (18) GPIO24
  GPIO10 (19) ○ ● (20) GND          ◀── alternative GND
   GPIO9 (21) ○ ○ (22) GPIO25
  GPIO11 (23) ○ ○ (24) GPIO8
     GND (25) ○ ○ (26) GPIO7
   GPIO0 (27) ○ ○ (28) GPIO1
   GPIO5 (29) ○ ○ (30) GND
   GPIO6 (31) ○ ○ (32) GPIO12
  GPIO13 (33) ○ ○ (34) GND
  GPIO19 (35) ○ ○ (36) GPIO16
  GPIO26 (37) ○ ○ (38) GPIO20
     GND (39) ○ ○ (40) GPIO21
```

### RUN pin driver circuit (recommended)

Driving the RP2040 RUN pad directly from an RPi4 GPIO pin can cause unreliable resets due to drive-strength and signal-integrity issues on the wire.
A simple NPN transistor circuit (originally described for [resetting a Raspberry Pi from a microcontroller](https://novamostra.com/2025/01/27/reset-raspberry-pi-using-a-raspberry-pico-or-arduino-microcontroller/)) was tested with a **BC337** (pin-compatible drop-in for the article's 2N3904) and works reliably.

```
RPi 3.3V ──[10 kΩ]──┬───────────── RUN pad (RP2040)
                     │                    │
                     │               [330 Ω]
                     │                    │
                     │               Collector
RPi GPIO ──[2.2 kΩ]──── Base    BC337 / 2N3904
                             Emitter
                                │
                               GND
```

| Component | Value | Purpose |
|---|---|---|
| Base resistor | 2.2 kΩ | Sets base current (~1.2 mA at 3.3 V logic) |
| Pull-up resistor | 10 kΩ | Holds RUN HIGH when transistor is off |
| Collector resistor | 330 Ω | Limits collector current when transistor is on |
| Transistor | BC337 or 2N3904 | NPN BJT switch |

The BC337 is pin-compatible with the 2N3904 in TO-92 packages with pin order E-B-C. Confirmed working with a BC337 and the existing `flash.py` code without any software changes.

### RP2040 pad locations

| Pad | Description | Behaviour |
|---|---|---|
| **RUN** | Active-low reset | Pull LOW → hold in reset; release HIGH → boot |
| **BOOTSEL** | Bootloader select | Pull LOW *while* RUN releases → enter USB mass-storage mode |

On a bare PolyKybd PCB (no keys fitted) these pads are accessible solder points on or near the RP2040 footprint.

### Flash sequence (what the software does)

```
1. uhubctl: power OFF the target USB port
2. GPIO: BOOTSEL → LOW
3. GPIO: RUN → LOW  (hold in reset)
4. GPIO: RUN → HIGH (release reset)  ← RP2040 sees BOOTSEL=LOW, enters bootloader
5. GPIO: BOOTSEL → HIGH
6. uhubctl: power ON the target USB port
7. Wait for /media/…/RPI-RP2 to appear
8. cp firmware.uf2 /media/…/RPI-RP2/
9. Wait 2.5 s for automatic reboot into new firmware
```

---

## USB Connections

| Cable | From | To | uhubctl port |
|---|---|---|---|
| USB cable | Left half | RPi4 USB-A | port 1 |
| USB cable | Right half | RPi4 USB-A | port 2 |
| Split cable | Left half | Right half | (always plugged in) |

The split cable stays connected permanently. During flashing the right half reboots briefly; the left half tolerates a momentary loss of the split peer.

Because both USB cables are always connected, set `MASTER_LEFT` in QMK's `config.h` so the firmware does not use `SPLIT_USB_DETECT` (which would be ambiguous with both sides powered).

---

## Software Setup

### 1. Clone

```bash
git clone https://github.com/thpoll83/polykybd-ctnd.git
cd polykybd-ctnd
```

### 2. Run the setup script

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh          # installs to /opt/polykybd-ctnd (recommended)
# or, to run the app from the current clone instead:
./scripts/setup.sh --local
```

> **Install path.** The default location is `/opt/polykybd-ctnd`, used throughout
> this README. With `--local` the app runs from wherever you cloned it (e.g.
> `/home/<user>/polykybd-ctnd`) — substitute that path in the commands below. The
> app derives its own location at runtime, so the on-screen runner diagnostics
> always quote the correct path regardless of where it's installed.

This will:
- Install `uhubctl`, `libhidapi`, `chromium` (or `chromium-browser` on older OS), Python 3 + venv
- Add the current user to the `gpio` and `plugdev` groups
- Install a udev rule for HID access without root
- Clone the repo to `/opt/polykybd-ctnd/` and create a venv (default), or use the current directory (`--local`)
- Install and enable the `polykybd-ctnd` and `polykybd-kiosk` systemd services with the correct username
- Print instructions for registering the GitHub Actions runner

### 3. Update config

Edit `config/config.yaml` (created automatically by `setup.sh` from the example):

```yaml
qmk:
  vendor_id:  0x????  # from keyboards/polykbd/config.h in qmk_firmware
  product_id: 0x????

usb:
  hub_location: "1-1"  # verify with: uhubctl
  left_port:  1
  right_port: 2

github:
  repo: "thpoll83/qmk_firmware"
  token: ""  # personal access token (PAT) — see below
  runner_labels: [self-hosted, polykybd-ctnd]  # must match runs-on: in qmk-test.yml
```

`config/config.yaml` is gitignored — `git pull` will never overwrite your settings.

**About `github.token` (optional but recommended).** A long-lived PAT unlocks three things:

- the **CI** header badge (needs `Actions: Read`),
- the **RUNNER** header badge + **⚕ Diagnose** runner status (needs `Administration: Read`),
- token-free **re-registration**: `register-runner.sh` mints its own short-lived registration token from this PAT, so you never copy a token from the GitHub UI again (needs `Administration: Read and write`).

Create a fine-grained PAT scoped to `thpoll83/qmk_firmware` with **Administration: Read and write** (and optionally **Actions: Read**) to enable all three. The PAT lives only in the gitignored `config.yaml`. Leave `token: ""` to skip these conveniences — registration then needs a manual `--token` (see §4).

### 4. Register GitHub Actions runner

**First-time install** — download and install the runner agent + systemd service once:

1. Go to `https://github.com/thpoll83/qmk_firmware/settings/actions/runners/new`
2. Select **Linux / ARM64**
3. Follow the download + configure steps shown
4. When prompted for labels, enter: `polykybd-ctnd`
5. Install as a service:
   ```bash
   sudo ./svc.sh install
   sudo ./svc.sh start
   ```

After this one-time install, you should **not** need to touch a token again — a configured runner survives reboots and firmware reflashes. If the registration ever breaks (`"waiting for a runner"`, `Not configured`, registration deleted), re-register with the helper script — and if you set a PAT in §3, it needs no token argument:

```bash
cd /opt/polykybd-ctnd
./scripts/register-runner.sh                 # PAT in config.yaml mints the token
./scripts/register-runner.sh --token <TOK>   # or paste a one-off registration token
```

Or recover straight from the **touchscreen** — no SSH — with the **Runner** row's **⟳ Restart** / **↻ Re-register** buttons (see [Troubleshooting](#github-actions-runner-waiting-for-a-runner)).

### 5. Reboot

```bash
sudo reboot
```

The touchscreen UI starts automatically. It is also accessible from any machine on your local network at `http://<rpi-ip>:5000`.

---

## Usage

### Touchscreen UI

| Control | Action |
|---|---|
| **Left / Right dropdowns** | Select a UF2 file from `/opt/polykybd-ctnd/firmware/` |
| **⟳ button** | Refresh the firmware file list |
| **Flash Left / Flash Right** | Flash a single half |
| **Run Tests** | Flash both halves then execute the test suite |
| **Clear Log / Copy Log** | Clear or copy the log panel |
| **Runner ⚕ Diagnose** | Print a full self-hosted-runner diagnostic to the log (see below) |
| **Runner ⟳ Restart** | `systemctl restart` the runner service — for a configured-but-wedged runner |
| **Runner ↻ Re-register** | Stop → reconfigure → restart the runner (two-tap confirm) — for a broken/`Not configured` registration |

The status dot in the header pulses amber while flashing, blue while testing, amber again while (re)registering the runner, and turns red on error.

**Header badges:**

- **CI** — turns green (`CI ✓`) when no job is running and orange (`CI ▶`) while a workflow runs (tap to open it). Polls every 60 s.
- **RUNNER** — at-a-glance self-hosted runner health (tap to run the diagnostic). Polls every 30 s:

  | Badge | Meaning |
  |---|---|
  | `RUNNER ✓` (green) | a runner with the required labels is online & idle |
  | `RUNNER ▶` (orange) | matching runner online but busy with a job |
  | `RUNNER ✕` (red) | matching runner registered but offline |
  | `RUNNER !` (red) | no runner advertises all required labels |
  | `RUNNER ⚿` (yellow) | the token can't read runner status (needs repo-admin scope) |

  The CI and RUNNER badges require a `github:` block in `config.yaml` (see [§3 Update config](#3-update-config)).

### Dropping firmware manually

Copy any `.uf2` file into `/opt/polykybd-ctnd/firmware/` — the UI picks it up on the next refresh.

### CI workflow

Copy `.github/workflows/qmk-test.yml` from this repo into `thpoll83/qmk_firmware/.github/workflows/`.
The workflow builds both halves on a cloud runner, uploads the UF2 artifacts, then runs the HIL test job on the `polykybd-ctnd` self-hosted runner.

---

## Troubleshooting

### Port 5000 not reachable after reboot

**Check if the service is running:**

```bash
sudo systemctl status polykybd-ctnd.service
```

If it shows `could not be found` the service was never installed — `setup.sh` did not complete successfully. Re-run it from the repo root (see below).

If it shows `failed`, inspect the logs:

```bash
sudo journalctl -u polykybd-ctnd -n 50
```

**Check whether anything is listening on port 5000:**

```bash
ss -tlnp | grep 5000
```

No output means the process is not running. If it shows `0.0.0.0:5000` the service is up; check your firewall (see below).

**Test locally on the Pi first:**

```bash
curl http://localhost:5000
```

If this works but a remote browser cannot connect, a firewall is blocking the port:

```bash
sudo ufw status
sudo ufw allow 5000/tcp
```

---

### `setup.sh` fails — `chromium-browser` not found

Raspberry Pi OS **Bookworm** (Debian 12) renamed the package to `chromium`. The current `setup.sh` detects this automatically. If you are on an older version of the script, update first:

```bash
git pull
./scripts/setup.sh
```

---

### `setup.sh` must be run from the repository root

The script copies the current directory into `/opt/polykybd-ctnd/`. Running it from the wrong location installs an empty or incorrect tree and the service will fail to start.

Always run it as:

```bash
cd ~/polykybd-ctnd   # the cloned repo root
chmod +x scripts/setup.sh
./scripts/setup.sh
```

---

### Service installed but failing — wrong username

The systemd service file contains a `User=` entry set at install time by `setup.sh`. If the service was installed while logged in as a different user, the unit may reference the wrong account.

Reinstall the service units for the current user without re-running the full setup:

```bash
cd ~/polykybd-ctnd
CTND_USER="${SUDO_USER:-$USER}"
CTND_HOME=$(getent passwd "$CTND_USER" | cut -d: -f6)

sed "s|User=pi|User=$CTND_USER|g; s|/home/pi|$CTND_HOME|g" \
  systemd/polykybd-ctnd.service | sudo tee /etc/systemd/system/polykybd-ctnd.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl restart polykybd-ctnd.service
```

---

### GitHub Actions runner: "waiting for a runner"

If a CI job sits at **"Waiting for a runner to pick up this job..."** forever, the self-hosted runner isn't matching `runs-on: [self-hosted, polykybd-ctnd]`. **Start with the diagnostic** — tap the **RUNNER** badge or the **⚕ Diagnose** button on the touchscreen. It reports, and gives a plain-language verdict for, the common causes (headless, check the same things with `systemctl status 'actions.runner.*'` and `journalctl -u 'actions.runner.*' -n 50`):

| What Diagnose shows | Cause | Fix |
|---|---|---|
| `actions.runner.*.service inactive/dead` + `An error occurred: Not configured` in the journal | Runner agent installed but never configured (no `.runner`/`.credentials`) | **Re-register** (below) — a restart alone won't help |
| service `inactive/dead`, otherwise configured | Runner crashed or was stopped | **Restart** (below) |
| runner OFFLINE on GitHub | Service down, or outbound HTTPS to `*.actions.githubusercontent.com` blocked | Restart; check network |
| `LABEL MISMATCH` / `RUNNER !` | No runner advertises both labels | Re-register (sets `--labels polykybd-ctnd`), or fix `runner_labels` in config |
| matching runner online & idle, job still queued | PR is **from a fork** | Open the run on GitHub → **Approve and run** |
| `RUNNER ⚿` / `403`/`401` | `github.token` missing or lacks `Administration: Read` | Add/scope the PAT (see [§3](#3-update-config)) |

**Restart** (configured but wedged) — just bounces the service:

```bash
cd /opt/polykybd-ctnd
./scripts/register-runner.sh --restart-only      # or tap ⟳ Restart on the touchscreen
```

**Re-register** (broken/`Not configured`/deleted registration) — full teardown → reconfigure → restart:

```bash
sudo git -C /opt/polykybd-ctnd pull
cd /opt/polykybd-ctnd
./scripts/register-runner.sh                     # PAT in config.yaml mints the token, or:
./scripts/register-runner.sh --token <TOKEN>     # paste a one-off token from the GitHub UI
```

…or tap **↻ Re-register** on the touchscreen (two-tap confirm). Get a manual token, if you need one, from `https://github.com/thpoll83/qmk_firmware/settings/actions/runners/new` → **Linux / ARM64**. Registration tokens expire in ~1 hour; a stored PAT (§3) avoids the copy-paste entirely.

> **Kiosk buttons need a one-time sudoers grant** so the UI service can control the runner. Fresh `setup.sh` runs add it automatically (`/etc/sudoers.d/polykybd-runner`, scoped to `systemctl start/stop/restart actions.runner.*`). On an existing box, re-run `setup.sh` or pull and restart. The **first-ever** registration still needs one SSH run (it installs the systemd unit); after that the touchscreen can recover it.

**Manual equivalent** (if the script is unavailable):
```bash
cd ~/actions-runner
sudo ./svc.sh stop
sudo ./svc.sh uninstall
rm -f .runner .credentials .credentials_rsaparams
./config.sh --url https://github.com/thpoll83/qmk_firmware \
            --token <TOKEN> --name RP4-HIL --labels polykybd-ctnd --replace --unattended
sudo ./svc.sh install
sudo ./svc.sh start
```

---

### Updating after a code change

`/opt/polykybd-ctnd` is a git clone, so pull and restart — no reboot or re-copy needed:

```bash
sudo git -C /opt/polykybd-ctnd pull
sudo systemctl restart polykybd-ctnd.service
sudo systemctl status polykybd-ctnd.service
```

If the update adds new Python dependencies, also run:

```bash
sudo /opt/polykybd-ctnd/venv/bin/pip install -q -r /opt/polykybd-ctnd/requirements.txt
```

---

## Project Structure

```
polykybd-ctnd/
├── station/
│   ├── config.py          GPIO pins, USB hub location, QMK HID IDs
│   ├── flash.py           FlashController — uhubctl + GPIO BOOTSEL
│   ├── hid.py             HIDConsole (QMK log reader) + RawHID
│   ├── test_runner.py     Orchestrator + CLI entry point
│   └── ui/
│       ├── app.py         Flask + SocketIO server
│       ├── templates/     index.html (1024×600 touch UI)
│       └── static/        style.css, app.js
├── systemd/
│   ├── polykybd-ctnd.service    Station daemon
│   └── polykybd-kiosk.service   Chromium kiosk
├── scripts/
│   ├── setup.sh              One-shot RPi setup
│   ├── register-runner.sh    Re-register the GitHub Actions self-hosted runner
│   └── kiosk.sh              Manual kiosk launch
└── .github/workflows/
    └── qmk-test.yml       Example CI workflow (copy to qmk_firmware)
```

---

## Related Repositories

| Repo | Description |
|---|---|
| [thpoll83/qmk_firmware](https://github.com/thpoll83/qmk_firmware) | PolyKybd QMK firmware fork |
| [thpoll83/PolyKybdHost](https://github.com/thpoll83/PolyKybdHost) | Host application (same Raw HID protocol) |

## License

GPL-2.0 — consistent with `qmk_firmware` and `PolyKybdHost`.
