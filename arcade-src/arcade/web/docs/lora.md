# LoRa: reaching an arcade by radio (T-Decks and Meshtastic)

Any arcade can run a **LoRa gateway**: a Meshtastic radio plugged into the arcade's machine, through which arcade
**T-Decks** (LilyGO T-Deck and T-Deck Plus running the arcade firmware) reach the arcade with no internet of their
own. A T-Deck signs in with its own key, sees its coins, sends, and reads and writes sealed messages; the gateway
carries each request over LoRa and talks to the arcade the way a browser does.

## For operators: running a gateway

Everything is on **Admin → Meshtastic**. The gateway is off until you turn it on.

1. Plug a Meshtastic radio (a Heltec V3, or any radio on stock Meshtastic firmware) into the arcade's machine by USB.
2. If the page says the radio support is missing, press **Install the radio support** at the node machine. It adds
   two Python packages, `meshtastic` and `coincurve`, to the arcade's own Python.
3. Pick the radio's **serial port** (`/dev/ttyUSB0`, `/dev/ttyACM0`, ...), give the gateway a **name** T-Decks will
   show, and tick the **coins the T-Decks see**.
4. Press **Run the gateway**.

The page then shows:

- **The radio:** its node id, region and preset, how many nodes it hears, and frames sent, acknowledged and failed.
- **The T-Decks** that have said hello: who is signed in, who is online, and mail waiting to go out to each.

The **beacon** is off by default. T-Decks find gateways by asking, and a beacon only adds airtime on a busy channel.

If the radio is unplugged or resets, the gateway opens it again by itself and keeps every T-Deck's session.

Linked arcades see a **LoRa** badge beside an arcade that runs a gateway, on Admin's overview.

## Flashing a T-Deck

**Admin → Meshtastic → Flash a T-Deck** writes the arcade firmware from the page over USB. The arcade fetches the
firmware release and checks its sha256 before your browser writes it.

The flasher uses Web Serial, so it needs:

- **Chrome or Edge on a computer.** Firefox has no Web Serial, and phones and tablets can't flash over USB from a
  browser.
- **The page opened over HTTPS, or on localhost.** Browsers turn Web Serial off on a plain `http://` address.
- On Linux, **Chromium from Ubuntu's App Center (a snap)** reaches USB only after
  `sudo snap connect chromium:raw-usb`, then a full restart of Chromium (every window). Firefox from the App Center
  can't reach USB at all. Google Chrome from google.com needs nothing.

For the first flash, on a T-Deck still running stock Meshtastic:

1. Start with the T-Deck switched off (switch up).
2. Hold the trackball down and plug in the USB cable.
3. Still holding the trackball, switch it on (switch down), wait 2 seconds, then let go. It is now in flash mode
   (the screen stays dark).
4. Press **Install the firmware** and pick the T-Deck's port (usually `USB JTAG/serial debug unit`). Only T-Decks
   are offered, never the gateway's radio.
5. When it says done, unplug the T-Deck and plug it in again, or press its reset.

After the first flash, the T-Deck goes into flash mode by itself: plug it in and press either button. **Update only**
writes just the app part, which is faster.

If the T-Deck can't be reached, a help box under the buttons says what to try on your system: the flash-mode steps
again, a cable that carries data (many USB-C cables only charge) plugged straight into the computer, the snap
commands above, Device Manager → Ports on Windows, and on Linux `sudo usermod -aG dialout $USER` when the port is
listed but won't open. Commands in the box have a **Copy** button.

The firmware is free software (GPL-3.0), built from Meshtastic's firmware and device-ui. Its source is at
[dogecoinarcade/firmware](https://github.com/dogecoinarcade/firmware/tree/arcade) and
[dogecoinarcade/device-ui](https://github.com/dogecoinarcade/device-ui/tree/arcade), branch `arcade`.
The gateway is [dogecoinarcade/arcade-lora](https://github.com/dogecoinarcade/arcade-lora).
