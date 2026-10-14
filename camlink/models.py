"""Which DJI Osmo camera is advertising, and whether it is awake, from its BLE advertisement.

DJI cameras advertise manufacturer data under company id 0x08AA starting with the model id (u16 LE).
The model ids below come from the MIT-licensed osmosis project (https://github.com/KonradIT/osmosis).
Only the Osmo Nano has been tested with these apps; the other Osmo models use the same transport and
may work fully or in part. The Osmo 360 is excluded (WPA3 Wi-Fi and 360 video).
"""

from __future__ import annotations

DJI_COMPANY_ID = 0x08AA

MODELS = {
    0x0010: "Osmo Action 2", 0x0012: "Osmo Action 3", 0x0014: "Osmo Action 4", 0x0015: "Osmo Action 5 Pro",
    0x0017: "Osmo 360", 0x0018: "Osmo Action 6", 0x0019: "Osmo Nano", 0x0020: "Osmo Pocket 3",
    0x0021: "Osmo Pocket 4", 0x0022: "Osmo Pocket 4 Pro",
}
TESTED_MODELS = {0x0019}
NANO = 0x0019
UNSUPPORTED_MODELS = {0x0017: "Osmo 360 (WPA3 Wi-Fi and 360° video: not supported)"}
# Newer adverts carry a product type that refines the model id.
PRODUCT_TYPES = {218: 0x0022}
NAME_PREFIXES = ("osmo", "xtra")
# Adverts without manufacturer data (scan responses): guess the model from the name, most specific first.
NAME_HINTS = (("pocket4pro", 0x0022), ("pocket4", 0x0021), ("pocket3", 0x0020), ("action6", 0x0018),
              ("action5", 0x0015), ("action4", 0x0014), ("action3", 0x0012), ("action2", 0x0010),
              ("osmo360", 0x0017), ("nano", 0x0019), ("atto", 0x0019), ("edgepro", 0x0015),
              ("edge", 0x0014), ("muse", 0x0020))
POWER_STATES = {2: "awake", 3: "asleep"}


def model_from_name(name: str) -> int | None:
    key = "".join(ch for ch in name.lower() if ch not in " -_")
    return next((model for hint, model in NAME_HINTS if hint in key), None)


def model_name(model: int | None) -> str:
    if model is None:
        return "DJI Osmo"
    return MODELS.get(model, f"DJI 0x{model:04x}")


def decode_advert(name: str, manufacturer_data: dict[int, bytes]) -> dict | None:
    """Model and power state of a DJI Osmo camera from its advertisement, or None for anything else.

    Layout seen on the Osmo Nano: <model u16> 40 <6-byte device address> <state>, where the state byte
    is 02 while the camera is awake and 03 while it sleeps."""
    data = manufacturer_data.get(DJI_COMPANY_ID)
    named = name.lower().startswith(NAME_PREFIXES)
    model = power = None
    if data and len(data) >= 2:
        model = int.from_bytes(data[:2], "little") or None
        if len(data) >= 12 and data[5] & 0x04:
            model = PRODUCT_TYPES.get(int.from_bytes(data[10:12], "little"), model)
        if len(data) >= 10:
            power = POWER_STATES.get(data[9], "unknown")
    if model is None:
        if not named:
            return None
        model = model_from_name(name)
    elif model not in MODELS and not named:
        return None                       # another DJI product (drone, remote controller...)
    return {"model_id": model, "model": model_name(model), "tested": model in TESTED_MODELS,
            "unsupported": UNSUPPORTED_MODELS.get(model), "power": power}


def liveview_mode(model_id: int | None) -> str:
    """How the datalink starts the live view: "nano" (verified on the Osmo Nano), or "action" for every
    other model: the Nano's commands plus the Action series' (see camlink.datalink)."""
    return "nano" if model_id == NANO else "action"
