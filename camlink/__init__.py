"""camlink: talk to DJI Osmo cameras from Windows, macOS or Linux over Bluetooth LE and Wi-Fi.

Unofficial and independent; not affiliated with DJI. The protocol is described in docs/PROTOCOL.md.

    protocol   addresses, identities and fixed payloads shared by the other modules
    duml       message framing (CRCs, encoder, stream reader)
    models     which camera is advertising, from its BLE advertisement
    ble        find, pair, wake the Wi-Fi and read the access point credentials
    wifi       join the camera's access point and return to the previous network (one backend per system)
    datalink   DUML commands and the H.264 live view over UDP
    video      H.264 decoding (PyAV)
    registry   cameras linked to this computer
"""

__version__ = "1.1.2"
