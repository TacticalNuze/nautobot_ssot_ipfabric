"""Diff sync shared adapter class attributes to synchronize applications."""

# Maps lower-case IPFabric vendor strings to their canonical Nautobot Manufacturer name.
# Add entries here if further vendor name mismatches are discovered.
VENDOR_NAME_MAP = {
    "check point": "Check Point",
    "checkpoint": "Check Point",
    "palo alto networks": "Palo Alto Networks",
    "palo alto": "Palo Alto Networks",
}


def normalize_vendor_name(vendor: str) -> str:
    """Return a canonical Manufacturer name for the given IPFabric vendor string.

    Falls back to str.capitalize() when no explicit mapping is found.
    """
    return VENDOR_NAME_MAP.get(vendor.lower(), vendor.capitalize())

from typing import ClassVar

from diffsync import Adapter

from nautobot_ssot.integrations.ipfabric.diffsync import diffsync_models


class DiffSyncModelAdapters(Adapter):
    """Nautobot adapter for DiffSync."""

    safe_delete_mode: ClassVar[bool] = True

    location = diffsync_models.Location
    device = diffsync_models.Device
    # interface = diffsync_models.Interface
    # vlan = diffsync_models.Vlan

    top_level = [
        "location",
    ]
