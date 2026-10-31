# WiFi Flasher

Flash STM32 chips over Wi-Fi through an ESP32-S3 running the
[`wifi-debugger` branch of openocd-on-esp32](https://github.com/filipembedded/openocd-on-esp32).
It works on Windows, Linux and macOS, and does not need GDB or an STM32 toolchain.

## Start it

- **Windows:** double-click `start.bat`.
- **Linux and macOS:** run `./start.sh`.

The app opens in your browser. Keep the small terminal window open while you use it,
and close it when you are done.

The starters need [uv](https://docs.astral.sh/uv/). `start.bat` offers to install it.
On Linux and macOS, install it first.

## Set up a new board

You only do this once for each ESP32-S3-DevKitC-1 N8R8 board.

1. Connect the board's **UART** USB port. On Windows you may need the
   [CP210x driver](https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers).
2. Open **Board setup**, select the USB port and click **Write firmware to board**.
3. Join the **esp-openocd** Wi-Fi network, which has no password.
4. Enter your network and click **Save Wi-Fi network**. It must be a 2.4 GHz network.
5. Rejoin your own network. On the **Flash** page, click **Find board**.

The firmware image is `firmware/` in this folder, or you can click **Choose image**.

## Wire a target

Connect the target's SWD header. Power the target separately, and do not connect 3.3 V.

| Signal | Target A | Target B |
|---|---|---|
| SWCLK | GPIO40 | GPIO41 |
| SWDIO | GPIO39 | GPIO42 |
| GND | GND | GND |

NRST is not used. Turn the target on **before** the board starts, or click
**Switch to this target** afterwards.

Keep the wires short, about 10 to 15 cm.

## Flash firmware

1. Select the target.
2. Drop an `.elf`, `.hex` or `.bin` file on the page.
3. Click **Flash**.

The app restarts the board when it has to change target, which takes about 10 seconds.
It verifies the flash contents with a CRC and starts the firmware.

## Change the Wi-Fi network

- **The board is on your network:** enter its address in **Board setup**, then click
  **Save Wi-Fi network**.
- **The board cannot reach its network:** after about a minute it starts the
  **esp-openocd** network. Join it and save the new network. Restarting the board
  tries the saved network again.
- **Nothing works:** connect USB and click **Clear saved network**.

## Terminal use

Every feature is also a command, run with `uv run wifi_flasher.py`:

```
uv run wifi_flasher.py scan
uv run wifi_flasher.py status --host 192.168.1.50
uv run wifi_flasher.py flash app.elf --target "Target A"
uv run wifi_flasher.py flash app.bin --config stm32g4x.cfg --pins 41,42 --base 0x08004000
uv run wifi_flasher.py select --target "Target B"
uv run wifi_flasher.py wifi MyNetwork --host 192.168.1.50
uv run wifi_flasher.py flash-board --port COM5 --image firmware/merged-binary.bin
uv run wifi_flasher.py forget-wifi --port COM5
```

Use `--no-browser` to start the GUI without opening a browser. Settings are saved in
`.wifi-flasher.json` in your home folder.

## Debugging

The board stays a normal GDB server, and any `arm-none-eabi-gdb` works, including ST's:

```
arm-none-eabi-gdb app.elf -ex "target extended-remote <board-ip>:3333"
```

Enter GDB commands one at a time, because some GDB builds treat a multi-line paste as
one command. After `detach`, the chip stays halted unless you run `monitor resume` first.

## Limits

- The ports have no authentication, so use the board only on a network you trust.
  Anyone on that network can reflash a connected target.
- The SWD clock is fixed at build time, about 120 kHz. `adapter speed` has no effect.
- **Find board** checks only your computer's /24 networks and the board's own network.
  On larger networks, enter the address yourself.
