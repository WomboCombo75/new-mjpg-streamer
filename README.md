# mjpg-streamer

Fork of [mjpg-streamer](http://sourceforge.net/projects/mjpg-streamer/) with a browser control UI (`streamctl_service.py`) for starting/stopping the stream and changing camera settings.

mjpg-streamer copies JPEG frames from input plugins (webcam, file, HTTP proxy, …) to output plugins (HTTP server, file, …). Viewers include Chrome, Firefox, VLC, and any app that accepts an MJPEG HTTP URL.

## Security warning

**Do not expose mjpg-streamer on untrusted networks.** Anyone who can reach the control port or stream URL can view (and, via the control UI, change) your camera stream.

Bind the control service to localhost when possible (`STREAMCTL_BIND=127.0.0.1` in `/etc/default/mjpg-streamctl`) and restrict access with a firewall or reverse proxy.

---

## Quick start (Raspberry Pi / Debian)

```bash
# 1. Dependencies (see table below for older distros)
sudo apt-get update
sudo apt-get install -y git cmake libjpeg-dev libjpeg62-turbo-dev gcc g++ make libv4l-dev

# 2. Clone and build
git clone https://github.com/WomboCombo75/new-mjpg-streamer.git
cd new-mjpg-streamer
make
sudo make install

# 3. Enable the web control UI at boot (recommended)
sudo ./scripts/install-streamctl-autostart.sh
```

Open in a browser:

| What | URL |
|------|-----|
| Control page | `http://<pi-ip>:8899/?html=1` |
| MJPEG stream (embed in apps) | `http://<pi-ip>:8899/mjpeg/?action=stream` |
| Snapshot | `http://<pi-ip>:8899/mjpeg/?action=snapshot` |

Use the control page to pick `/dev/video0` (or another device), resolution, FPS, and start the stream.

---

## Build dependencies

### Debian 12 / 13, Raspberry Pi OS Bookworm / Trixie (recommended)

`libjpeg8-dev` **no longer exists** on current Debian. Use:

```bash
sudo apt-get install -y cmake libjpeg-dev libjpeg62-turbo-dev gcc g++ make libv4l-dev
```

| Package | Purpose |
|---------|---------|
| `cmake`, `gcc`, `g++`, `make` | Build system |
| `libjpeg-dev` | JPEG headers/libs (metapackage) |
| `libjpeg62-turbo-dev` | Actual libjpeg-turbo development files |
| `libv4l-dev` | Video4Linux — needed for `input_uvc` (USB / libcamera webcams) |

Optional packages (only if you need those plugins):

```bash
# OpenCV input plugin
sudo apt-get install -y libopencv-dev

# SDL viewer output plugin
sudo apt-get install -y libsdl1.2-dev

# PTP2 / gPhoto2 input plugin
sudo apt-get install -y libgphoto2-dev
```

### Older Debian / Raspberry Pi OS (Bullseye and earlier)

```bash
sudo apt-get install -y cmake libjpeg8-dev gcc g++ make libv4l-dev
```

---

## Which camera plugin do I need?

| Camera | Plugin | Notes |
|--------|--------|-------|
| USB webcam | `input_uvc` | Built on all systems with `libv4l-dev` |
| Raspberry Pi Camera Module (libcamera stack) | `input_uvc` | Appears as `/dev/video0`, `/dev/video1`, … — use the control page device list |
| Legacy Pi camera (MMAL, `/opt/vc`) | `input_raspicam` | Only built if `/opt/vc/include` exists (old Raspberry Pi userland). **Not available** on modern Pi OS without legacy stack |

After `make`, CMake prints which plugins were enabled or skipped. You only need `input_uvc` + `output_http` for typical USB/libcamera setups.

List video devices:

```bash
ls -l /dev/video*
v4l2-ctl --list-devices   # from package v4l-utils (optional)
```

---

## Build and install

From the repository root:

```bash
make
sudo make install
```

This installs:

- `/usr/local/bin/mjpg_streamer`
- Plugins under `/usr/local/lib/mjpg-streamer/`
- Static web files under `/usr/local/share/mjpg-streamer/www/`

The top-level `Makefile` also copies `mjpg_streamer` and `*.so` into the repo directory so you can run without installing:

```bash
export LD_LIBRARY_PATH=.
./mjpg_streamer -i "input_uvc.so -d /dev/video0" -o "output_http.so -w ./www"
```

### Debug build

```bash
make distclean
make CMAKE_BUILD_TYPE=Debug
sudo make install
```

### Advanced (CMake options)

```bash
mkdir _build && cd _build
cmake -DENABLE_HTTP_MANAGEMENT=ON ..
make
sudo make install
```

See individual plugin READMEs under `plugins/` for plugin-specific options.

---

## Stream control webapp (autostart)

This fork includes **`streamctl_service.py`**: a small HTTP service with a browser UI to start/stop the stream and change device, resolution, FPS, and HTTP port.

- Control UI listens on **8899** by default (`STREAMCTL_BIND`, `STREAMCTL_PORT`).
- The MJPEG HTTP server from `output_http` runs on **8080** by default, bound to **localhost**.
- Stream pages are **proxied** at `http://<host>:8899/mjpeg/` so you only expose one port.

### Install and enable at boot

Run from the **repo root** (paths are recorded in the systemd unit at install time):

```bash
sudo ./scripts/install-streamctl-autostart.sh
```

If you **move or rename the clone**, reinstall so the unit points at the new directory:

```bash
cd ~/new-mjpg-streamer   # your actual clone path
sudo ./scripts/install-streamctl-autostart.sh
```

### Service commands

```bash
sudo systemctl status mjpg-streamctl
sudo systemctl restart mjpg-streamctl
sudo systemctl stop mjpg-streamctl
sudo systemctl disable --now mjpg-streamctl   # remove from boot and stop
```

### Optional configuration

Create or edit `/etc/default/mjpg-streamctl`:

```bash
# Listen on localhost only (safer on LAN)
STREAMCTL_BIND=127.0.0.1
STREAMCTL_PORT=8899

# Optional API token (see streamctl_service.py)
# STREAMCTL_TOKEN=change-me
```

Then: `sudo systemctl restart mjpg-streamctl`

The unit template is `systemd/mjpg-streamctl.service.in`; the install script generates `/etc/systemd/system/mjpg-streamctl.service`.

### Run manually (no systemd)

```bash
cd /path/to/new-mjpg-streamer
python3 streamctl_service.py
```

Open `http://<pi-ip>:8899/?html=1`.

---

## Usage examples

### USB / libcamera webcam (installed binary)

```bash
/usr/local/bin/mjpg_streamer \
  -i "input_uvc.so -d /dev/video0" \
  -o "output_http.so -w /usr/local/share/mjpg-streamer/www"
```

Stream: `http://<pi-ip>:8080/?action=stream`

### Legacy Raspberry Pi camera (`input_raspicam`, if built)

```bash
export LD_LIBRARY_PATH=/usr/local/lib/mjpg-streamer
/usr/local/bin/mjpg_streamer \
  -i "input_raspicam.so -x 1280 -y 720 -fps 15" \
  -o "output_http.so -w /usr/local/share/mjpg-streamer/www"
```

See [plugins/input_raspicam/README.md](plugins/input_raspicam/README.md) for raspicam options.

### With streamctl (recommended)

Use the control page — no manual command line needed. Embed the proxied URL:

```html
<img src="http://<pi-ip>:8899/mjpeg/?action=stream" alt="Live stream" />
```

**VLC:** Media → Open Network Stream → `http://<pi-ip>:8899/mjpeg/?action=stream`

---

## Troubleshooting

### `apt-get install libjpeg8-dev` fails

On Debian 12+ / current Raspberry Pi OS, that package was removed. Install `libjpeg-dev` and `libjpeg62-turbo-dev` instead (see [Build dependencies](#build-dependencies)).

### `make` succeeds but `input_raspicam` is disabled

Expected on systems without legacy `/opt/vc` (MMAL). Use **`input_uvc`** with your `/dev/video*` device instead.

### No `/dev/video*` devices

- Enable the camera in `raspi-config` (Pi Camera Module).
- For USB: check `lsusb`, try another port/cable.
- Install `v4l-utils` and run `v4l2-ctl --list-devices`.

### Control page does not load after boot

```bash
sudo systemctl status mjpg-streamctl
journalctl -u mjpg-streamctl -n 50 --no-pager
```

Common fixes:

- Re-run `sudo ./scripts/install-streamctl-autostart.sh` after moving the repo.
- Try `http://127.0.0.1:8899/?html=1` instead of `localhost` (IPv6 quirks).
- Ensure `python3` is installed.

### Stream URL works on the Pi but not from another machine

- Confirm `STREAMCTL_BIND` is not `127.0.0.1` if you need LAN access (default in the generated unit is `0.0.0.0`).
- Check firewall: `sudo ufw status` or allow port 8899.

### Blank or frozen browser preview

- Lower resolution/FPS in the control page.
- Try snapshot URL first: `http://<pi-ip>:8899/mjpeg/?action=snapshot`
- Check another viewer (VLC) to isolate browser issues.

---

## Plugins

### Input

| Plugin | Description |
|--------|-------------|
| `input_file` | Read JPEG files from disk |
| `input_http` | HTTP input proxy |
| `input_uvc` | Video4Linux (USB webcams, libcamera V4L2) — [docs](plugins/input_uvc/README.md) |
| `input_raspicam` | Legacy Pi camera (MMAL) — [docs](plugins/input_raspicam/README.md) |
| `input_opencv` | OpenCV — [docs](plugins/input_opencv/README.md) |
| `input_ptp2` | PTP2 cameras |

### Output

| Plugin | Description |
|--------|-------------|
| `output_http` | HTTP MJPEG server — [docs](plugins/output_http/README.md) |
| `output_file` | Write JPEG files |
| `output_viewer` | SDL viewer — [docs](plugins/output_viewer/README.md) |
| `output_zmqserver` | ZMQ — [docs](plugins/output_zmqserver/README.md) |
| `output_rtsp` / `output_udp` | Present in tree; not fully functional |

---

## Discussion

Historical thread: [Raspberry Pi forum](http://www.raspberrypi.org/phpBB3/viewtopic.php?f=43&t=45178)

## Authors

mjpg-streamer was originally created by Tom Stöveken, with contributions from many others.

## License

GNU General Public License v2. See [LICENSE](www/LICENSE.txt) in the bundled web assets and plugin sources for details.
