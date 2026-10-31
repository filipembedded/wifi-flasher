#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "esptool>=5,<6",
#     "intelhex>=2.3",
#     "pyelftools>=0.31",
#     "pyserial>=3.5",
# ]
# ///
"""Flash STM32 targets over Wi-Fi through an ESP32-S3 running openocd-on-esp32.

Run without arguments for the GUI, or use a subcommand from a terminal.
"""

import argparse
import concurrent.futures
import getpass
import html
import ipaddress
import json
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = Path.home() / ".wifi-flasher.json"
FLASH_BASE = 0x08000000
AP_HOST = "192.168.4.1"
RPC_PORT = 6666
GDB_PORT = 3333
NVS_OFFSET, NVS_SIZE = 0x9000, 0x6000
# GPIOs on the N8R8 DevKitC-1 that are free of flash, PSRAM, USB, UART and straps.
FREE_GPIOS = {1, 2, *range(4, 19), 21, 38, 39, 40, 41, 42, 47, 48}
NON_TARGET_CONFIGS = re.compile(r"^(esp|xtensa-core-)|^swj-dp\.tcl$|^stm32x5x_common\.cfg$")
ADDRESS_LINE = re.compile(rb"\bIP\s*:\s*(\d+\.\d+\.\d+\.\d+)")
UNKNOWN_PROGRESS = -1
DEFAULT_SETTINGS = {
    "host": "",
    "selected": 0,
    "targets": [
        {"name": "Target A", "cfg": "stm32f4x.cfg", "swclk": 40, "swdio": 39},
        {"name": "Target B", "cfg": "stm32l4x.cfg", "swclk": 41, "swdio": 42},
    ],
}


class FlasherError(Exception):
    """An error with a message meant for the person using the tool."""


def load_settings():
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text())}
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_SETTINGS))


def save_settings(settings):
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


# Firmware files


@dataclass
class Segment:
    addr: int
    data: bytes

    @property
    def end(self):
        return self.addr + len(self.data)


def merge_segments(segments):
    merged = []
    for segment in sorted(segments, key=lambda s: s.addr):
        if merged and segment.addr < merged[-1].end:
            raise FlasherError(f"Firmware sections overlap at 0x{segment.addr:08x}.")
        if merged and segment.addr == merged[-1].end:
            merged[-1] = Segment(merged[-1].addr, merged[-1].data + segment.data)
        else:
            merged.append(segment)
    return merged


def read_elf(path):
    from elftools.elf.elffile import ELFFile

    with path.open("rb") as file:
        elf = ELFFile(file)
        if elf["e_machine"] != "EM_ARM":
            raise FlasherError(f"{path.name} is not built for an ARM chip.")
        return [
            Segment(segment["p_paddr"], segment.data())
            for segment in elf.iter_segments()
            if segment["p_type"] == "PT_LOAD" and segment["p_filesz"]
        ]


def read_hex(path):
    from intelhex import IntelHex

    image = IntelHex(str(path))
    return [
        Segment(start, image.tobinarray(start=start, size=end - start).tobytes())
        for start, end in image.segments()
    ]


def load_firmware(path, base=FLASH_BASE):
    """Read an .elf, .hex or .bin file into the bytes to place in flash."""
    path = Path(path)
    try:
        magic = path.read_bytes()[:4]
    except OSError as err:
        raise FlasherError(f"Cannot read {path.name}: {err.strerror}.") from err
    try:
        if magic == b"\x7fELF":
            segments = read_elf(path)
        elif path.suffix.lower() == ".hex":
            segments = read_hex(path)
        else:
            segments = [Segment(base, path.read_bytes())]
    except FlasherError:
        raise
    except Exception as err:  # the parsers raise many types on malformed files
        raise FlasherError(f"{path.name} is not a valid firmware file.") from err
    segments = merge_segments(segments)
    if not segments:
        raise FlasherError(f"{path.name} contains nothing to flash.")
    return segments


# GDB remote protocol, which OpenOCD uses for flash programming


def crc32_gdb(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]
    return crc


def _crc_entry(index):
    crc = index << 24
    for _ in range(8):
        crc = ((crc << 1) ^ 0x04C11DB7 if crc & 0x80000000 else crc << 1) & 0xFFFFFFFF
    return crc


_CRC_TABLE = [_crc_entry(i) for i in range(256)]


def escape_binary(data):
    out = bytearray()
    for byte in data:
        if byte in b"#$}*":
            out += bytes((0x7D, byte ^ 0x20))
        else:
            out.append(byte)
    return bytes(out)


class GdbRemote:
    def __init__(self, host, port=GDB_PORT, timeout=20):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as err:
            raise FlasherError(
                f"Cannot reach the debug server on {host}. "
                "Check that the target is powered and shows as ready."
            ) from err
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.stream = self.sock.makefile("rb")
        self.acks = True
        self.packet_size = 1024

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.sock.close()

    def _read(self, size=1):
        try:
            data = self.stream.read(size)
        except OSError as err:
            raise FlasherError("The debugger stopped responding.") from err
        if not data:
            raise FlasherError(
                "The debugger closed the connection. The target may not be ready."
            )
        return data

    def _read_packet(self):
        while self._read() != b"$":
            pass
        payload = bytearray()
        while (byte := self._read()) != b"#":
            payload += byte
        self._read(2)
        if self.acks:
            self.sock.sendall(b"+")
        return bytes(payload)

    def _send(self, payload):
        checksum = sum(payload) & 0xFF
        self.sock.sendall(b"$" + payload + b"#%02x" % checksum)
        if self.acks:
            while self._read() != b"+":
                pass

    def command(self, payload, timeout=20):
        """Send a packet and return its reply, skipping console output."""
        self.sock.settimeout(timeout)
        self._send(payload if isinstance(payload, bytes) else payload.encode())
        self.output = bytearray()
        while True:
            reply = self._read_packet()
            if reply.startswith(b"O") and reply != b"OK":
                self.output += bytes.fromhex(reply[1:].decode())
            else:
                return reply

    def checked(self, payload, what, timeout=20):
        reply = self.command(payload, timeout)
        if reply != b"OK":
            raise FlasherError(f"{what} failed on the target. Please try again.")

    def monitor(self, text, timeout=30):
        self.checked(f"qRcmd,{text.encode().hex()}", f"'{text}'", timeout)
        return self.output.decode(errors="replace")

    def handshake(self):
        reply = self.command("qSupported:multiprocess+;swbreak+;hwbreak+")
        if match := re.search(rb"PacketSize=([0-9a-fA-F]+)", reply):
            self.packet_size = int(match[1], 16)
        if b"QStartNoAckMode+" in reply and self.command("QStartNoAckMode") == b"OK":
            self.acks = False

    def flash_regions(self):
        """Return (start, end, block size) for every flash region of the target."""
        xml, offset = b"", 0
        while True:
            reply = self.command(f"qXfer:memory-map:read::{offset:x},400")
            if reply[:1] not in (b"m", b"l"):
                raise FlasherError("The target did not report its memory map.")
            xml += reply[1:]
            offset += len(reply) - 1
            if reply[:1] == b"l":
                break
        regions = []
        for memory in ElementTree.fromstring(xml).iter("memory"):
            if memory.get("type") == "flash":
                start = int(memory.get("start"), 16)
                block = int(memory.find("property").text, 16)
                regions.append((start, start + int(memory.get("length"), 16), block))
        return sorted(regions)


def erase_ranges(segments, regions):
    """Return the block-aligned ranges that cover the firmware."""
    ranges = []
    for segment in segments:
        position = segment.addr
        while position < segment.end:
            region = next((r for r in regions if r[0] <= position < r[1]), None)
            if region is None:
                raise FlasherError(
                    f"Firmware uses 0x{position:08x}, which is outside the target's "
                    "flash. Is it built for this chip?"
                )
            start, end, block = region
            first = start + (position - start) // block * block
            last = min(segment.end, end)
            last = start + -((start - last) // block) * block
            ranges.append((first, last))
            position = last
    merged = []
    for first, last in sorted(ranges):
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def flash_firmware(host, segments, report):
    size = sum(len(segment.data) for segment in segments)
    with GdbRemote(host) as gdb:
        gdb.handshake()
        # Escaping can double the data, and the header needs some room
        chunk = min(2048, (gdb.packet_size - 64) // 2)
        report("Preparing the target", 5)
        gdb.monitor("reset halt")
        ranges = erase_ranges(segments, gdb.flash_regions())
        report(f"Erasing {sum(b - a for a, b in ranges) // 1024} KiB", 10)
        for first, last in ranges:
            gdb.checked(f"vFlashErase:{first:x},{last - first:x}", "Erase", 180)
        report(f"Sending {size / 1024:.1f} KiB", 30)
        sent = 0
        for segment in segments:
            for offset in range(0, len(segment.data), chunk):
                data = segment.data[offset : offset + chunk]
                header = f"vFlashWrite:{segment.addr + offset:x}:".encode()
                gdb.checked(header + escape_binary(data), "Sending data", 30)
                sent += len(data)
                report(None, 30 + 20 * sent // size)
        report("Writing to flash", 50)
        gdb.checked("vFlashDone", "Writing", 60 + size // 1000)
        report("Verifying", 85)
        for segment in segments:
            packet = f"qCRC:{segment.addr:x},{len(segment.data):x}"
            reply = gdb.command(packet, 60 + len(segment.data) // 2000)
            if reply != b"C%x" % crc32_gdb(segment.data):
                raise FlasherError(f"Verification failed at 0x{segment.addr:08x}.")
        report("Starting the firmware", 97)
        # "reset run" races the firmware's startup and leaves debug reads returning zeros
        gdb.monitor("reset halt")
        gdb.monitor("resume")
        gdb.sock.sendall(b"$D#44")
    report("Done", 100)


# ESP32 board


def http_call(host, path, body=None, timeout=5, restarts=False):
    request = urllib.request.Request(
        f"http://{host}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        raise FlasherError(f"The board rejected the request: {err.reason}.") from err
    except (OSError, ValueError) as err:
        if restarts:
            return b""
        raise FlasherError(f"Cannot reach the board at {host}.") from err


def get_config(host, timeout=5):
    try:
        config = json.loads(http_call(host, "/get_openocd_config", timeout=timeout))
    except ValueError as err:
        raise FlasherError(f"{host} is not an OpenOCD board.") from err
    if "configList" not in config:
        raise FlasherError(f"{host} is not an OpenOCD board.")
    return config


def target_configs(config):
    return sorted(c for c in config["configList"] if not NON_TARGET_CONFIGS.search(c))


def configured_pins(config):
    match = re.search(r"esp_gpio_swd_nums\s+(\d+)\s+(\d+)", config.get("cParam", ""))
    return (int(match[1]), int(match[2])) if match else None


def rpc(sock, command):
    sock.sendall(command.encode() + b"\x1a")
    reply = b""
    while not reply.endswith(b"\x1a"):
        data = sock.recv(4096)
        if not data:
            raise OSError("closed")
        reply += data
    return reply[:-1].decode(errors="replace").strip()


def probe_debugger(host, timeout=3):
    """Ask OpenOCD whether it runs and can talk to the target."""
    status = {"openocd": False, "examined": False, "state": "unknown"}
    try:
        with socket.create_connection((host, RPC_PORT), timeout=timeout) as sock:
            sock.settimeout(timeout)
            name = rpc(sock, "target names").split()[0]
            status["openocd"] = True
            if rpc(sock, f"{name} was_examined") != "1":
                # Reads are not perfectly reliable, so one failed attempt proves little
                rpc(sock, f"catch {{{name} arp_examine}}")
            status["examined"] = rpc(sock, f"{name} was_examined") == "1"
            status["state"] = rpc(sock, f"{name} curstate")
            status["target"] = name
    except (OSError, IndexError):
        pass
    return status


def wait_for_board(host, config_name, timeout=45):
    deadline = time.monotonic() + timeout
    time.sleep(4)
    while time.monotonic() < deadline:
        try:
            if get_config(host, 3).get("target") == config_name:
                return
        except FlasherError:
            pass
        time.sleep(1)
    raise FlasherError(f"The board did not come back at {host} after restarting.")


def wait_for_target(host, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_debugger(host)["examined"]:
            return True
        time.sleep(1)
    return False


def select_target(host, target, report=lambda *_: None):
    """Point the debugger at a target, restarting the board only if needed."""
    pins = (target["swclk"], target["swdio"])
    config = get_config(host)
    active = (
        config.get("target") == target["cfg"]
        and configured_pins(config) == pins
        and config.get("interface") == "1"
    )
    for attempt in range(3):
        if active and attempt == 0:
            if probe_debugger(host)["examined"]:
                return
        report(f"Selecting {target['name']} (the board restarts)", 2)
        http_call(
            host,
            "/set_openocd_config",
            {
                "target": target["cfg"],
                "interface": "1",
                "rtos": config.get("rtos", "none"),
                "debug": "2",
                "dualCore": True,
                "flash": True,
                "cParam": f"esp_gpio_swd_nums {pins[0]} {pins[1]}",
            },
            restarts=True,
        )
        wait_for_board(host, target["cfg"])
        if wait_for_target(host):
            return
    raise FlasherError(
        f"{target['name']} is not responding. Check that it is powered and that "
        f"SWCLK is on GPIO{pins[0]}, SWDIO on GPIO{pins[1]} and GND is connected."
    )


def flash_target(host, target, path, report=lambda *_: None, base=FLASH_BASE):
    segments = load_firmware(path, base)
    select_target(host, target, report)
    for attempt in (1, 2):
        try:
            flash_firmware(host, segments, report)
            return
        except FlasherError as err:
            if attempt == 2:
                raise
            report(f"{err} Retrying once", 5)
            select_target(host, target, report)


def validate_target(target):
    pins = (target.get("swclk"), target.get("swdio"))
    if pins[0] == pins[1]:
        raise FlasherError("SWCLK and SWDIO must use different GPIOs.")
    for pin in pins:
        if pin not in FREE_GPIOS:
            raise FlasherError(f"GPIO{pin} cannot be used for SWD on this board.")


def local_hosts():
    """List the addresses of this computer's /24 networks and the board's own AP."""
    addresses = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    hosts = [AP_HOST]
    for address in addresses:
        if not address.startswith("127."):
            network = ipaddress.ip_network(f"{address}/24", strict=False)
            hosts += [str(host) for host in network.hosts() if str(host) != address]
    return list(dict.fromkeys(hosts))


def discover():
    def check(host):
        try:
            get_config(host, 2)
            return host
        except FlasherError:
            return None

    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        return [host for host in pool.map(check, local_hosts()) if host]


def board_status(host):
    try:
        config = get_config(host, 4)
    except FlasherError as err:
        return {"online": False, "message": str(err)}
    pins = configured_pins(config)
    return {
        "online": True,
        "configs": target_configs(config),
        "config": config.get("target"),
        "pins": pins,
        **probe_debugger(host),
    }


def set_wifi(host, ssid, password):
    http_call(host, "/set_credentials", {"ssid": ssid, "pass": password}, restarts=True)


def list_ports():
    from serial.tools import list_ports as serial_ports

    return [
        {"device": port.device, "label": f"{port.device} - {port.description}"}
        for port in serial_ports.comports()
        if port.vid is not None
    ]


def run_esptool(port, arguments):
    command = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port]
    result = subprocess.run(
        command + arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    if result.returncode:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        raise FlasherError(f"esptool failed: {' '.join(lines[-3:])}")


def default_image():
    images = sorted((APP_DIR / "firmware").glob("*.bin"))
    return images[0] if images else None


def read_board_address(port, report, restart=True, timeout=90):
    """Return the address the board prints on its USB serial port while it starts."""
    import serial

    link = serial.Serial()
    link.port, link.baudrate, link.timeout = port, 115200, 0.2
    link.dtr = link.rts = False
    try:
        link.open()
    except serial.SerialException as err:
        raise FlasherError(f"Cannot open {port}. Close any serial monitor using it.") from err
    with link:
        report("Waiting for the board to connect", 90)
        if restart:
            link.rts = True
            time.sleep(0.15)
            link.rts = False
        deadline = time.monotonic() + timeout
        output = b""
        while time.monotonic() < deadline:
            output = output[-200:] + link.read(4096)
            if match := ADDRESS_LINE.search(output):
                return match[1].decode()
    raise FlasherError(
        "The board did not report an address. Check that it is an N8R8 module, "
        "and look for the esp-openocd Wi-Fi network."
    )


def report_address(address, report):
    network = " on the esp-openocd Wi-Fi network" if address == AP_HOST else ""
    report(f"Board address: {address}{network}", 100)
    return {"address": address}


def show_board_address(port, report, restart=True):
    return report_address(read_board_address(port, report, restart), report)


def flash_board(port, image, report):
    # esptool hides its progress when its output is not a terminal
    report("Writing the debugger firmware, about one minute", UNKNOWN_PROGRESS)
    run_esptool(port, ["--baud", "460800", "write-flash", "0x0", str(image)])
    return show_board_address(port, report)


def forget_wifi(port, report):
    report("Clearing the saved network", UNKNOWN_PROGRESS)
    run_esptool(port, ["erase-region", hex(NVS_OFFSET), hex(NVS_SIZE)])
    return show_board_address(port, report)


# GUI: a local page served only to this computer


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False, "stage": "", "percent": 0, "error": None,
                      "log": [], "result": None}

    def start(self, work):
        with self.lock:
            if self.state["running"]:
                raise FlasherError("Another operation is still running.")
            self.state = {"running": True, "stage": "Starting", "percent": 0,
                          "error": None, "log": [], "result": None}
        threading.Thread(target=self._run, args=(work,), daemon=True).start()

    def report(self, stage, percent):
        with self.lock:
            if stage:
                self.state["stage"] = stage
                self.state["log"].append(stage)
            self.state["percent"] = percent

    def _run(self, work):
        error = result = None
        try:
            result = work(self.report)
        except FlasherError as err:
            error = str(err)
        except Exception as err:  # keep the GUI usable after an unexpected failure
            error = f"Unexpected error: {err}"
        with self.lock:
            self.state.update(running=False, error=error, result=result)
            if error:
                self.state["log"].append(error)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))


class Handler(BaseHTTPRequestHandler):
    token = ""
    job = Job()
    allowed_hosts = ()
    upload_dir = ""

    def log_message(self, *_):
        pass

    def respond(self, payload, status=200, content_type="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return (
            self.headers.get("Host") in self.allowed_hosts
            and secrets.compare_digest(self.headers.get("X-Token", ""), self.token)
        )

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/":
            page = (APP_DIR / "ui.html").read_text(encoding="utf-8")
            page = page.replace("__TOKEN__", html.escape(self.token))
            self.respond(page.encode(), content_type="text/html; charset=utf-8")
        elif not self.authorized():
            self.respond({"error": "Forbidden"}, 403)
        elif url.path == "/api/state":
            image = default_image()
            self.respond({"settings": load_settings(), "job": self.job.snapshot(),
                          "image": image.name if image else None, "pins": sorted(FREE_GPIOS)})
        elif url.path == "/api/job":
            self.respond(self.job.snapshot())
        elif url.path == "/api/ports":
            self.respond(list_ports())
        else:
            self.respond({"error": "Not found"}, 404)

    def read_body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def store_upload(self, body):
        name = urllib.parse.unquote(self.headers.get("X-Filename", "firmware.bin"))
        path = Path(self.upload_dir) / Path(name).name
        path.write_bytes(body)
        return path

    def do_POST(self):
        if not self.authorized():
            self.respond({"error": "Forbidden"}, 403)
            return
        route = urllib.parse.urlsplit(self.path).path
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        body = self.read_body()
        try:
            result = self.dispatch(route, query, body)
        except FlasherError as err:
            self.respond({"error": str(err)}, 400)
        except (ValueError, KeyError, IndexError) as err:
            self.respond({"error": f"Invalid request: {err}"}, 400)
        except Exception as err:  # the page must always get an answer
            self.respond({"error": f"Unexpected error: {err}"}, 500)
        else:
            self.respond(result)

    def dispatch(self, route, query, body):
        if route == "/api/settings":
            settings = json.loads(body)
            for target in settings["targets"]:
                validate_target(target)
            save_settings(settings)
            return {"ok": True}
        if route == "/api/discover":
            return {"hosts": discover()}
        if route == "/api/status":
            return board_status(json.loads(body)["host"])
        settings = load_settings()
        if route == "/api/flash":
            target = settings["targets"][int(query["target"])]
            path = self.store_upload(body)
            base = int(query.get("base", hex(FLASH_BASE)), 16)
            load_firmware(path, base)
            self.job.start(lambda r: flash_target(settings["host"], target, path, r, base))
        elif route == "/api/select":
            target = settings["targets"][int(query["target"])]
            self.job.start(lambda r: (select_target(settings["host"], target, r),
                                      r(f"{target['name']} is ready", 100)))
        elif route == "/api/wifi":
            data = json.loads(body)
            set_wifi(data["host"], data["ssid"], data["password"])
        elif route == "/api/board/flash":
            image = self.store_upload(body) if body else default_image()
            if image is None:
                raise FlasherError("Choose a firmware image for the board first.")
            self.job.start(lambda r: flash_board(query["port"], image, r))
        elif route == "/api/board/forget-wifi":
            self.job.start(lambda r: forget_wifi(query["port"], r))
        elif route == "/api/board/address":
            restart = query.get("restart") != "0"
            self.job.start(lambda r: show_board_address(query["port"], r, restart))
        else:
            raise FlasherError("Unknown request.")
        return {"ok": True}


def run_gui(port, open_browser):
    Handler.token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual_port = server.server_address[1]
    Handler.allowed_hosts = (f"127.0.0.1:{actual_port}", f"localhost:{actual_port}")
    with tempfile.TemporaryDirectory(prefix="wifi-flasher-") as upload_dir:
        Handler.upload_dir = upload_dir
        url = f"http://127.0.0.1:{actual_port}/"
        print(f"WiFi Flasher is running at {url}\nClose this window to quit.", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


# Terminal interface


def print_progress(stage, percent):
    if stage:
        print(stage if percent == UNKNOWN_PROGRESS else f"[{percent:3d}%] {stage}", flush=True)


def resolve_host(args):
    host = args.host or load_settings()["host"]
    if host:
        return host
    hosts = discover()
    if len(hosts) != 1:
        raise FlasherError("Use --host, since " + (
            f"several boards were found: {', '.join(hosts)}." if hosts else "no board was found."))
    return hosts[0]


def resolve_target(args):
    if args.config:
        swclk, swdio = (int(pin) for pin in args.pins.split(","))
        target = {"name": args.config, "cfg": args.config, "swclk": swclk, "swdio": swdio}
        validate_target(target)
        return target
    targets = load_settings()["targets"]
    for target in targets:
        if target["name"].lower() == args.target.lower():
            return target
    names = ", ".join(target["name"] for target in targets)
    raise FlasherError(f"Unknown target '{args.target}'. Saved targets: {names}.")


def command_scan(_):
    hosts = discover()
    print("\n".join(hosts) if hosts else "No board found.")
    return 0 if hosts else 1


def command_status(args):
    host = resolve_host(args)
    print(json.dumps({"host": host, **board_status(host)}, indent=2))
    return 0


def command_flash(args):
    host = resolve_host(args)
    flash_target(host, resolve_target(args), args.file, print_progress, int(args.base, 16))
    return 0


def command_select(args):
    select_target(resolve_host(args), resolve_target(args), print_progress)
    print("Target is ready.")
    return 0


def command_wifi(args):
    set_wifi(args.host or load_settings()["host"] or AP_HOST, args.ssid,
             getpass.getpass(f"Password for {args.ssid}: "))
    print("Saved. The board is restarting.")
    return 0


def command_flash_board(args):
    image = args.image or default_image()
    if image is None:
        raise FlasherError("Pass the firmware image with --image.")
    flash_board(args.port, image, print_progress)
    return 0


def command_forget_wifi(args):
    forget_wifi(args.port, print_progress)
    return 0


def command_address(args):
    show_board_address(args.port, print_progress)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0, help="local GUI port")
    parser.add_argument("--no-browser", action="store_true", help="do not open the GUI page")
    commands = parser.add_subparsers(dest="command")

    def add_board_args(sub):
        sub.add_argument("--host", help="board address (default: saved or discovered)")

    def add_target_args(sub):
        sub.add_argument("--target", default="Target A", help="saved target name")
        sub.add_argument("--config", help="OpenOCD target config, instead of --target")
        sub.add_argument("--pins", default="40,39", help="SWCLK,SWDIO GPIOs for --config")

    commands.add_parser("gui", help="open the GUI (default)")
    commands.add_parser("scan", help="find boards on this network").set_defaults(run=command_scan)
    sub = commands.add_parser("status", help="show board and target state")
    add_board_args(sub)
    sub.set_defaults(run=command_status)
    sub = commands.add_parser("flash", help="flash firmware to a target")
    sub.add_argument("file", help=".elf, .hex or .bin firmware")
    sub.add_argument("--base", default=hex(FLASH_BASE), help="load address for .bin files")
    add_board_args(sub)
    add_target_args(sub)
    sub.set_defaults(run=command_flash)
    sub = commands.add_parser("select", help="switch the debugger to a target")
    add_board_args(sub)
    add_target_args(sub)
    sub.set_defaults(run=command_select)
    sub = commands.add_parser("wifi", help="change the board's Wi-Fi network")
    sub.add_argument("ssid")
    add_board_args(sub)
    sub.set_defaults(run=command_wifi)
    sub = commands.add_parser("flash-board", help="write the debugger firmware over USB")
    sub.add_argument("--port", required=True, dest="port")
    sub.add_argument("--image", help="merged firmware image")
    sub.set_defaults(run=command_flash_board)
    sub = commands.add_parser("forget-wifi", help="erase saved settings over USB")
    sub.add_argument("--port", required=True, dest="port")
    sub.set_defaults(run=command_forget_wifi)
    sub = commands.add_parser("address", help="show the board's address over USB")
    sub.add_argument("--port", required=True, dest="port")
    sub.set_defaults(run=command_address)
    return parser


def main():
    args = build_parser().parse_args()
    if args.command in (None, "gui"):
        run_gui(args.port, not args.no_browser)
        return 0
    try:
        return args.run(args)
    except FlasherError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
