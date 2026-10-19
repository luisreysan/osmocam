"""The errors of camlink.wifi, in a module of their own so that the system backends can raise them too."""

from __future__ import annotations


class WifiError(RuntimeError):
    """Joining the camera's network failed; the message says why and what to do."""


class NoWifi(WifiError):
    """This computer has no Wi-Fi adapter it can use: trying again won't help."""


class JoinByHand(WifiError):
    """This computer can't move its Wi-Fi by itself (Linux without NetworkManager): the user joins the
    camera's network by hand, and the apps wait for that (on_camera_network())."""
