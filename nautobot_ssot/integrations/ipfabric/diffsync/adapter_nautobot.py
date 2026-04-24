# pylint: disable=duplicate-code
# pylint: disable=too-many-arguments
# Load method is packed with conditionals  #  pylint: disable=too-many-branches
"""DiffSync adapter class for Nautobot as source-of-truth."""

import logging
from collections import defaultdict
from typing import Any, ClassVar, List, Optional

from diffsync import Adapter
from diffsync.exceptions import ObjectAlreadyExists
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, Q
from nautobot.core.choices import ColorChoices
from nautobot.dcim.models import Device, Location
from nautobot.extras.models import Tag
from nautobot.ipam.models import VLAN, Interface
from netutils.ip import cidr_to_netmask
from netutils.mac import mac_to_format

from nautobot_ssot.integrations.ipfabric.constants import (
    CUSTOM_ROLES,
    DEFAULT_INTERFACE_MAC,
    DEFAULT_INTERFACE_MTU,
    SYNC_IPF_DEV_TYPE_TO_ROLE,
    SAFE_DELETE_DEVICE_STATUS,
)
from nautobot_ssot.integrations.ipfabric.diffsync import DiffSyncModelAdapters

logger = logging.getLogger("nautobot.ssot.ipfabric")


class NautobotDiffSync(DiffSyncModelAdapters):
    """Nautobot adapter for DiffSync."""

    objects_to_delete = defaultdict(list)

    _vlan: ClassVar[Any] = VLAN
    _device: ClassVar[Any] = Device
    _location: ClassVar[Any] = Location
    _interface: ClassVar[Any] = Interface

    def __init__(
        self,
        job,
        sync,
        sync_ipfabric_tagged_only: bool,
        location_filter: Optional[Location],
        *args,
        **kwargs,
    ):
        """Initialize the NautobotDiffSync."""
        super().__init__(*args, **kwargs)
        self.job = job
        self.sync = sync
        self.sync_ipfabric_tagged_only = sync_ipfabric_tagged_only
        self.location_filter = location_filter

    def sync_complete(self, source: Adapter, *args, **kwargs):
        """Clean up function for DiffSync sync.

        Once the sync is complete, this function runs deleting any objects
        from Nautobot that need to be deleted in a specific order.

        Args:
            source (Adapter): DiffSync Adapter
        """
        for grouping in (
            "_device",
            "_location",
        ):
            for nautobot_object in self.objects_to_delete[grouping]:
                if NautobotDiffSync.safe_delete_mode:
                    continue
                try:
                    nautobot_object.delete()
                except ProtectedError:
                    logger.warning("Deletion failed protected object", extra={"object": nautobot_object})
                except IntegrityError:
                    logger.warning(f"Deletion failed due to IntegrityError with {nautobot_object}")

            self.objects_to_delete[grouping] = []
        return super().sync_complete(source, *args, **kwargs)

    def load_interfaces(self, device_record: Device, diffsync_device):
        """Import a single Nautobot Interface object as a DiffSync Interface model."""
        device_primary_ip = None
        if device_record.primary_ip4:
            device_primary_ip = device_record.primary_ip4
        elif device_record.primary_ip6:
            device_primary_ip = device_record.primary_ip6

        for interface_record in device_record.interfaces.all():
            ip_address_obj = interface_record.ip_addresses.first()
            if ip_address_obj:
                ip_address = ip_address_obj.host
                subnet_mask = cidr_to_netmask(ip_address_obj.mask_length)
            else:
                ip_address = None
                subnet_mask = None
            interface = self.interface(
                status=device_record.status.name,
                name=interface_record.name,
                device_name=device_record.name,
                description=interface_record.description if interface_record.description else None,
                enabled=True,
                mac_address=(
                    mac_to_format(str(interface_record.mac_address), "MAC_COLON_TWO").upper()
                    if interface_record.mac_address
                    else DEFAULT_INTERFACE_MAC
                ),
                subnet_mask=subnet_mask,
                mtu=interface_record.mtu if interface_record.mtu else DEFAULT_INTERFACE_MTU,
                type=interface_record.type,
                mgmt_only=interface_record.mgmt_only if interface_record.mgmt_only else False,
                pk=interface_record.pk,
                ip_is_primary=ip_address_obj == device_primary_ip if device_primary_ip else False,
                ip_address=ip_address,
            )
            self.add(interface)
            diffsync_device.add_child(interface)

    def load_device(self, filtered_devices: List, location):
        """Load Devices from Nautobot."""
        for device_record in filtered_devices:
            if not device_record.serial:
                if self.job.debug:
                    logger.debug(f"Skipping Nautobot Device due to missing serial: {device_record.name}")
                continue

            if "/" in device_record.serial:
                fixed_serial = device_record.serial.split("/", 1)[0].upper()
                self.job.logger.info(
                    f"Fixing serial number for {device_record.name} in Nautobot database: {device_record.serial} -> {fixed_serial}"
                )
                device_record.serial = fixed_serial
                # Using update() to avoid triggering validations/signals during load phase, 
                # but fixing the underlying data so DiffSync identifiers match.
                Device.objects.filter(pk=device_record.pk).update(serial=fixed_serial)
            elif device_record.serial != device_record.serial.upper():
                # Normalize existing serials to uppercase so they match IPFabric's normalized output.
                fixed_serial = device_record.serial.upper()
                self.job.logger.info(
                    f"Normalizing serial case for {device_record.name} in Nautobot database: {device_record.serial} -> {fixed_serial}"
                )
                device_record.serial = fixed_serial
                Device.objects.filter(pk=device_record.pk).update(serial=fixed_serial)

            if device_record.status.name == SAFE_DELETE_DEVICE_STATUS and device_record.tags.filter(name="SSoT Safe Delete").exists():
                if self.job.debug:
                    logger.debug(
                        f"Skipping Nautobot Device '{device_record.name}' as it is already marked for Safe Delete."
                    )
                continue

            # Mirror the platform filter applied on the IPFabric side.
            # vCMP devices are skipped in the IPFabric adapter; without this matching
            # filter the Nautobot adapter would load them and DiffSync would soft-delete them.
            if device_record.platform and device_record.platform.name.lower() == "vcmp":
                if self.job.debug:
                    logger.debug(
                        f"Skipping Nautobot Device '{device_record.name}' — platform 'vcmp' is excluded from sync."
                    )
                continue

            # Mirror the CUSTOM_ROLES filter applied on the IPFabric side.
            # Devices whose role is not in CUSTOM_ROLES must be invisible to both adapters
            # so DiffSync never treats them as "Nautobot-only" and triggers safe-delete
            # (which would change their status to offline).
            if CUSTOM_ROLES:
                device_role = (
                    str(device_record.role.cf.get("ipfabric_type"))
                    if device_record.role.cf.get("ipfabric_type")
                    else device_record.role.name
                )
                network_prefixed = (
                    f"network_{device_role}"
                    if not device_role.startswith("network_")
                    else device_role
                )
                if device_role not in CUSTOM_ROLES and network_prefixed not in CUSTOM_ROLES:
                    if self.job.debug:
                        logger.debug(
                            f"Skipping Nautobot Device '{device_record.name}' — role '{device_role}' not in CUSTOM_ROLES."
                        )
                    continue
            device_role = (
                str(device_record.role.cf.get("ipfabric_type"))
                if device_record.role.cf.get("ipfabric_type")
                else device_record.role.name
            )

            # Resolve the IPFabric-level location that owns this device.
            # IPFabric only knows about site-level locations (not racks/floors/sub-locations).
            # We walk up the Nautobot location hierarchy until we reach a location that has
            # the 'SSoT Synced from IPFabric' tag (i.e. a location synced from IPFabric),
            # or the absolute root if none is found (safe fallback for first-time syncs).
            # This handles arbitrary hierarchy depths, e.g.:
            #   Site 1 → Site 1.1 → Site 1.1.a (SSoT tag set) → Rack → Device
            # Without this, the naive root-walk would land at "Site 1" when IPFabric
            # reports the device under "Site 1.1.a", causing location_name mismatches.
            root_location = device_record.location
            while root_location.parent:
                if root_location.tags.filter(name="SSoT Synced from IPFabric").exists():
                    break  # This is an IPFabric-synced site — stop here
                root_location = root_location.parent

            device = self.device(
                name=device_record.name,
                model=str(device_record.device_type),
                role=device_role if SYNC_IPF_DEV_TYPE_TO_ROLE else None,
                location_name=root_location.name,
                vendor=str(device_record.device_type.manufacturer),
                status=device_record.status.name,
                serial_number=device_record.serial,
            )
            if device_record.platform:
                device.platform = device_record.platform.name
            if device_record.virtual_chassis:
                device.vc_name = device_record.virtual_chassis.name
                device.vc_position = device_record.vc_position
                device.vc_priority = device_record.vc_priority
                device.vc_master = bool(device_record.virtual_chassis.master == device_record)
            try:
                self.add(device)
            except ObjectAlreadyExists:
                # Expected when a rack-mounted device's serial has already been loaded
                # because load_device is called for both the rack and its parent site.
                if self.job.debug:
                    logger.debug(
                        f"Device '{device_record.name}' already loaded (likely via a parent/sub-location), skipping."
                    )
                continue

    def load_vlans(self, filtered_vlans: List, location):
        """Add Nautobot VLAN objects as DiffSync VLAN models."""
        for vlan_record in filtered_vlans:
            if not vlan_record:
                continue
            vlan = self.vlan(
                name=vlan_record.name,
                location=vlan_record.location.name,
                status=vlan_record.status.name if vlan_record.status else "Active",
                vid=vlan_record.vid,
                vlan_pk=vlan_record.pk,
                description=vlan_record.description,
            )
            try:
                self.add(vlan)
            except ObjectAlreadyExists:
                logger.warning(f"Duplicate VLAN discovered, {vlan_record.name}")
                continue
            location.add_child(vlan)

    def get_initial_location(self, ssot_tag: Tag):
        """Identify the location objects based on user defined job inputs.

        Args:
            ssot_tag (Tag): Tag used for filtering
        """
        # Simple check / validate Tag is present.
        if self.sync_ipfabric_tagged_only:
            location_objects = Location.objects.filter(tags__name=ssot_tag.name)
            if self.location_filter:
                location_objects = Location.objects.filter(
                    Q(name=self.location_filter.name) & Q(tags__name=ssot_tag.name)
                )
                if not location_objects:
                    logger.warning(
                        f"{self.location_filter.name} was used to filter, alongside SSoT Tag. {self.location_filter.name} is not tagged."
                    )
        elif not self.sync_ipfabric_tagged_only:
            if self.location_filter:
                location_objects = Location.objects.filter(name=self.location_filter.name)
            else:
                location_objects = Location.objects.all()
        return location_objects

    @transaction.atomic
    def load_data(self):
        """Add Nautobot Location objects as DiffSync Location models."""
        ssot_tag, _ = Tag.objects.get_or_create(
            name="SSoT Synced from IPFabric",
            defaults={
                "description": "Object synced at some point from IPFabric to Nautobot",
                "color": ColorChoices.COLOR_LIGHT_GREEN,
            },
        )
        location_objects = self.get_initial_location(ssot_tag)
        # The parent object that stores all children, is the Location.
        if self.job.debug:
            logger.debug("Found %s Nautobot Location objects to start sync from", location_objects.count())

        if location_objects:
            
            diffsync_locations = []
            
            for location_record in location_objects:
                try:
                    location = self.location(
                        name=location_record.name,
                        site_id=location_record.custom_field_data.get("ipfabric_site_id"),
                        status=location_record.status.name,
                        location_type=location_record.location_type.name,
                        parent_name=location_record.parent.name if location_record.parent else None,
                    )
                except AttributeError:
                    logger.error(
                        "Error loading %s, invalid or missing attributes on object. Skipping...", location_record
                    )
                    continue
                diffsync_locations.append((location_record, location))
                
            for index, (location_record, location) in enumerate(diffsync_locations):
                try:
                    self.add(location)
                except ObjectAlreadyExists:
                    logger.warning(f"Duplicate Location discovered, {location_record.name}")
                    location = self.get(self.location, location_record.name)
                    diffsync_locations[index] = (location_record, location)
            
            for location_record, location in diffsync_locations:
                if location.parent_name:
                    try:
                        parent_loc = self.get(self.location, location.parent_name)
                        parent_loc.add_child(location)
                    except Exception as e:
                        logger.warning(f"Parent location {location.parent_name} not found in diffsync tree for Nautobot site {location.name}. Error: {e}")
                
                try:
                    # Load Location's Children - Devices with Interfaces, if any.
                    # Fetch descendants using django-tree-queries to ensure devices in sub-locations/racks are loaded
                    descendant_locations = location_record.descendants(include_self=True)
                    if self.sync_ipfabric_tagged_only:
                        nautobot_location_devices = Device.objects.filter(
                            Q(location__in=descendant_locations) & Q(tags__name=ssot_tag.name)
                        )
                    else:
                        nautobot_location_devices = Device.objects.filter(location__in=descendant_locations)
                    if nautobot_location_devices.exists():
                        self.load_device(nautobot_location_devices, location)
                except Location.DoesNotExist:
                    logger.error("Unable to find Location, %s.", location_record)
        else:
            logger.warning("No Nautobot records to load.")

    def load(self):
        """Load data from Nautobot."""
        self.load_data()
