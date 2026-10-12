# UISP NMS for Home Assistant

*Unofficial: not made, supported or endorsed by Ubiquiti.*

Monitor and control the devices managed by a self-hosted UISP (UNMS) server — EdgeRouters, EdgeSwitches and other Ubiquiti devices — through UISP's API with a single API token. No device passwords are stored in Home Assistant.

## What you get

Per device:
- **Connected to UISP** (binary sensor; stays available when the device is unreachable)
- **CPU usage**, **Memory usage**, **Last boot**, and **Temperature** where the device reports one

Per physical port (switch ports, router `ethN`; not VLANs or LAGs):
- **Link** (binary sensor) with negotiated speed, port description and PoE output as attributes
- **RX rate** / **TX rate** (Mbit/s), with error and drop counters as attributes
- **PoE** switch on ports that can supply power
- **PoE output** select on ports offering more than one output (e.g. ES-8-150W: off / active 48V / 24V passive)
- **Enabled** switch to turn the port on or off — *disabled by default*, see below

Service **`uisp_nms.power_cycle_port`**: turns a port's PoE off, waits (default 5 s) and turns it back on with the same output — handy for a stuck access point or camera.

## Safety

- **PoE voltage is never guessed.** Turning a PoE switch on restores the output the port last used (remembered across restarts). On ports with several outputs and no known last output, turning on is refused; pick the output with the **PoE output** select. Passive 24V into a device that expects 802.3af/at (or the reverse) can damage it or leave it unpowered.
- **PoE can power other switches.** If a switch is powered from another switch's PoE port, turning that PoE off (or power-cycling it) takes down the downstream switch and everything behind it.
- **Port enable switches are disabled by default.** Disabling the port a device uses to reach UISP cuts it off, and it can then only be re-enabled locally on the device. Enable only the ones you need, in **Settings → Devices & services → UISP NMS → entities**.

## Requirements

- Home Assistant 2026.2 or later (tested on 2026.2.3 and 2026.10.0)
- A self-hosted UISP server (tested with UISP 3.1.65: ER-4, ES-10X, ES-8-150W, ES-24-Lite)
- A UISP API token (**UISP → Settings → Users → API Tokens**, as an admin). A read-only token is enough for monitoring; PoE and port control need a token with write access.

## Install

1. In HACS, add `https://github.com/jeffgkd705/ha-uisp-nms` as a custom repository (type: Integration) and install **UISP NMS**. Or copy `custom_components/uisp_nms` into your Home Assistant `config/custom_components/` folder.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → UISP NMS**. Enter the UISP URL (e.g. `https://uisp.example.com`) and the API token.
4. If UISP uses its default self-signed certificate, you are shown its SHA-256 fingerprint. Check it matches your server (for example in the browser's certificate viewer) and submit to trust exactly that certificate. If the certificate is ever replaced, the integration stops connecting until you confirm the new one with **Reconfigure**.

## Notes

- UISP is polled every 30 seconds: one request for the device list plus one per connected device. After a change, the port is re-read 5 seconds later, because UISP answers before the device has applied it. A change takes about 3 seconds to go through UISP.
- Data rates come from UISP's `rxrate`/`txrate` (bits per second).
- The integration's icon ships in `custom_components/uisp_nms/brand/` and is shown by Home Assistant 2026.3 or later; older versions show a placeholder. It is an original design, not Ubiquiti's logo.
- Avoid opening UISP's API documentation page (`/nms/api-docs`) on small servers: generating it took over 100 seconds of CPU on UISP 3.1.65, made UISP restart its API and disconnected all devices.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```

Tests run against an in-memory fake of the UISP API (`tests/conftest.py`).

## Acknowledgements

This integration started from [marosmars/edgeswitch-homeassistant](https://github.com/marosmars/edgeswitch-homeassistant) by Maros Marsalek, which controls EdgeSwitches through their own web API. Its structure was the model for this one, and the `power_cycle_port` action is adapted from its `cycle_port` service. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for its license.

## License

MIT. See [LICENSE](LICENSE).
