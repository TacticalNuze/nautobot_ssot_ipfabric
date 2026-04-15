# pylint: disable=duplicate-code
"""DiffSync adapter class for Ip Fabric."""

import ipaddress
import logging
from collections import defaultdict

from diffsync import ObjectAlreadyExists
from nautobot.dcim.models import Device, Location as NautobotLocation
from nautobot.ipam.models import VLAN
from netutils.interface import canonical_interface_name
from netutils.mac import mac_to_format

from nautobot_ssot.integrations.ipfabric.constants import (
    DEFAULT_DEVICE_ROLE,
    DEFAULT_DEVICE_STATUS,
    DEFAULT_INTERFACE_MAC,
    DEFAULT_INTERFACE_MTU,
    IP_FABRIC_USE_CANONICAL_INTERFACE_NAME,
    SYNC_IPF_DEV_TYPE_TO_ROLE,
    CUSTOM_LOCATIONS,
    CUSTOM_ROLES,)
from nautobot_ssot.integrations.ipfabric.diffsync import DiffSyncModelAdapters
from nautobot_ssot.integrations.ipfabric.diffsync.adapters_shared import normalize_vendor_name
from nautobot_ssot.integrations.ipfabric.utilities import utils as ipfabric_utils
from nautobot_ssot.integrations.ipfabric.utilities.site_parser import parse_site_hierarchy
from nautobot_ssot.integrations.ipfabric.utilities.virtual_machine_parser import parse_virtual_machine_name

try:
    from ipfabric import IPFClient
except ImportError:
    IPFClient = None


logger = logging.getLogger("nautobot.jobs")

device_serial_max_length = Device._meta.get_field("serial").max_length
name_max_length = VLAN._meta.get_field("name").max_length

# pylint: disable=too-many-locals,too-many-nested-blocks,too-many-branches
class IPFabricDiffSync(DiffSyncModelAdapters):
    """IPFabric adapter for DiffSync."""

    def __init__(self, job, sync, client: IPFClient, location_filter, *args, **kwargs):
        """Initialize the NautobotDiffSync."""
        super().__init__(*args, **kwargs)
        self.job = job
        self.sync = sync
        self.client = client
        if location_filter:
            self.client.attribute_filters = {"siteName": ["ieq", location_filter]}
            logging.info("Applied IP Fabric Attribute Filter: %s", self.client.attribute_filters)

    def load_sites(self):
        """Add IP Fabric Location objects as DiffSync Location models."""
        sites = self.client.inventory.sites.all()

        #self.job.logger.info(f"The locations from ipfabric are {sites}.")
        locations=CUSTOM_LOCATIONS #format {"siteName": 1-location_name }
        self.job.logger.info(f"The locations from config are {locations}.")
        try:
            for site in locations:
                sites.append(site)
               
        except Exception as e:
            self.job.logger.error(f"Found exception during addition of sites. Error message {e}")
        try:
            self.job.logger.info(f"Starting the site hierarchy construction")
            parsed_hierarchy = parse_site_hierarchy(sites)
        except Exception as e:
            self.job.logger.error(f"Found exception during parsing of sites. Error message {e}")
        
        def site_depth(site_obj):
            site_name = site_obj.get("siteName", "")
            data = parsed_hierarchy.get(site_name, {})
            pt = data.get("prefix_tuple")
            return len(pt) if pt else 0

        sorted_sites = sorted(sites, key=site_depth)

        for site in sorted_sites:
            site_name = site["siteName"]
            hierarchy_data = parsed_hierarchy.get(site_name, {})
            parent_name = hierarchy_data.get("parent_name")
            location_type = hierarchy_data.get("location_type", "Site")
            
            # Preserve existing Nautobot location type to prevent duplicate/recreated locations
            try:
                existing_loc = NautobotLocation.objects.filter(name=site_name).first()
                if existing_loc and existing_loc.location_type:
                    location_type = existing_loc.location_type.name
            except Exception as e:
                logger.warning(f"Could not retrieve existing location type for {site_name}: {e}")

            try:
                location = self.location(
                    adapter=self, 
                    name=site_name, 
                    site_id=site.get("id", ""), 
                    status="Active",
                    location_type=location_type,
                    parent_name=parent_name
                )
                self.add(location)
                if parent_name:
                    try:
                        parent_loc = self.get(self.location, parent_name)
                        parent_loc.add_child(location)
                    except Exception as e:
                        logger.warning(f"Parent location {parent_name} not found in diffsync tree for dict site {site_name}. Error: {e}")
            except ObjectAlreadyExists:
                logger.warning(f"Duplicate Location discovered, {site}")

    def load_device_interfaces(self, device_model, device_interfaces, device_primary_ip, managed_ipv4):
        """Create and load DiffSync Interface model objects for a specific device."""
        pseudo_interface = pseudo_management_interface(device_model.name, device_interfaces, device_primary_ip)

        if pseudo_interface:
            device_interfaces.append(pseudo_interface)
            logger.info("Pseudo MGMT Interface: %s", pseudo_interface)

        for iface in device_interfaces:
            # loginIpv4 is available in 7.3+, fallback to primaryIp for older versions
            if ip_address := iface.get("primaryIp") or iface.get("loginIpv4"):
                if ip_address in managed_ipv4 and managed_ipv4[ip_address].get("net"):
                    subnet_mask = str(ipaddress.ip_interface(managed_ipv4[ip_address]["net"]).netmask)
                else:
                    subnet_mask = "255.255.255.255"
            else:
                subnet_mask = None

            iface_name = iface["intName"]
            if IP_FABRIC_USE_CANONICAL_INTERFACE_NAME:
                iface_name = canonical_interface_name(iface_name)
            try:
                interface = self.interface(
                    name=iface_name,
                    device_name=iface.get("hostname"),
                    description=iface.get("dscr", ""),
                    enabled=True,
                    mac_address=(
                        mac_to_format(iface.get("mac"), "MAC_COLON_TWO").upper()
                        if iface.get("mac")
                        else DEFAULT_INTERFACE_MAC
                    ),
                    mtu=iface.get("mtu") if iface.get("mtu") else DEFAULT_INTERFACE_MTU,
                    type=ipfabric_utils.convert_media_type(iface.get("media"), iface_name),
                    mgmt_only=iface.get("mgmt_only", False),
                    ip_address=ip_address,
                    subnet_mask=subnet_mask,
                    ip_is_primary=ip_address is not None and ip_address == device_primary_ip,
                    status="Active",
                )
                self.add(interface)
                device_model.add_child(interface)
            except ObjectAlreadyExists:
                logger.warning(f"Duplicate Interface discovered, {iface}")

    def load_data(self):
        """Load shared data from IP Fabric."""
        managed_ipv4 = defaultdict(dict)
        stacks, interfaces = defaultdict(list), defaultdict(list)
        part_numbers = {}  # {sn: part_number} — built once to avoid per-device API calls

        vlans = self.client.fetch_all("tables/vlan/site-summary")

        ip_columns = ["sn", "intName", "net", "ip", "type"]
        ip_filter = {"type": ["eq", "primary"]}

        for ip_address in self.client.technology.addressing.managed_ip_ipv4.all(columns=ip_columns, filters=ip_filter):
            managed_ipv4[ip_address["sn"]].update({ip_address["ip"]: ip_address})

        # Get all interfaces for devices
        for interface in self.client.inventory.interfaces.all():
            interfaces[interface["sn"]].append(interface)

        # Get all stacks for devices
        for stack in self.client.technology.platforms.stacks_members.all(
            columns=["master", "member", "memberSn", "pn", "sn"]
        ):
            stacks[stack["sn"]].append(stack)

        # Bulk-fetch part numbers in a single API call instead of calling device.pn() per device.
        # The IPFabric SDK exposes pn as a callable that makes an individual request each time
        # it is invoked, which causes N API calls (one per device) and can overload the server.
        try:
            for row in self.client.inventory.devices.all(columns=["sn", "pn"]):
                if row.get("sn"):
                    part_numbers[row["sn"]] = str(row.get("pn") or "")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Could not bulk-fetch part numbers from IPFabric; part_number will be empty: %s", exc)

        return managed_ipv4, vlans, stacks, interfaces, part_numbers

    def load(self):  # pylint: disable=too-many-locals,too-many-statements
        """Load data from IP Fabric."""
        self.load_sites()

        managed_ipv4, _, stacks, _, part_numbers = self.load_data()

        for location in self.get_all(self.location):
            if location.name is None:
                continue
            for device in self.client.devices.by_site.get(location.name, []):
                if device.platform.lower() == 'vcmp':
                    self.job.logger.info(f"Skipping import for device {device.hostname} with platform vcmp.")
                    continue
                
                device_role = device.dev_type or DEFAULT_DEVICE_ROLE if SYNC_IPF_DEV_TYPE_TO_ROLE else None
                # Check mapping with "network_" prefix since it's added during DiffSync save
                network_prefixed_role = f"network_{device_role}" if device_role and not device_role.startswith("network_") else device_role
                if CUSTOM_ROLES and (device_role not in CUSTOM_ROLES and network_prefixed_role not in CUSTOM_ROLES):
                    self.job.logger.info(f"Skipping import for device {device.hostname} as its role '{device_role}' is not in custom_roles.")
                    continue

                # Look up part number from the bulk-fetched dict (keyed by serial number).
                # This avoids calling device.pn() per device which makes one API request each time.
                _part_number = part_numbers.get(device.sn, "")

                base_args = {
                    "location_name": device.site,
                    "model": device.model or f"Default-{device.vendor}",
                    "vendor": normalize_vendor_name(device.vendor),
                    "role": device.dev_type or DEFAULT_DEVICE_ROLE if SYNC_IPF_DEV_TYPE_TO_ROLE else None,
                    "status": DEFAULT_DEVICE_STATUS,
                    "platform": device.family,
                    "part_number": _part_number,
                }
                if device.sn not in stacks:
                    parsed_name, _ = parse_virtual_machine_name(device.hostname, device.sn)
                    # Use the raw IPFabric serial as-is (includes /XXXX suffix) to preserve uniqueness
                    raw_serial = device.sn or ""
                    args = base_args.copy()
                    args["name"] = parsed_name
                    args["serial_number"] = raw_serial if len(raw_serial) < device_serial_max_length else ""
                    member_devices = [args]
                else:
                    # member with the lowest member number will be considered master,
                    # and vc_priority and vc_position will both be derived from the member field,
                    # as the role field will depend on operational state and not config,
                    # and this will cause uneccessary diffs.
                    stack_members = stacks[device.sn]
                    stack_members.sort(key=lambda x: x["member"])
                    member_devices = []
                    for index, member in enumerate(stack_members):
                        # using `or` syntax in case memberSn is defined as None
                        member_sn = member.get("memberSn") or ""
                        parsed_name, _ = parse_virtual_machine_name(device.hostname, member_sn)
                        # Use the raw stack member serial as-is to preserve uniqueness (e.g. SERIAL/XXXX)
                        raw_member_sn = member_sn
                        args = base_args.copy()
                        if _ := member.get("pn"):
                            args["model"] = _
                            args["part_number"] = _
                        args.update(
                            {
                                "serial_number": raw_member_sn if len(raw_member_sn) < device_serial_max_length else "",
                                "name": f"{parsed_name}-{member.get('member')}",
                                "vc_name": parsed_name,
                                "vc_master": False,
                                "vc_priority": member.get("member"),
                                "vc_position": member.get("member"),
                            }
                        )
                        if index == 0:
                            args.update(
                                {
                                    "name": f"{parsed_name}-1",
                                    "vc_master": True,
                                }
                            )
                        member_devices.append(args)

                for index, dev in enumerate(member_devices):
                    if not dev["serial_number"]:
                        self.job.logger.warning(
                            f"Serial Number is missing or exceeds max length for {dev['name']}. Skipping device import."
                        )
                        continue
                    try:
                        device_model = self.device(**dev)
                        self.add(device_model)
                        location.add_child(device_model)
                    except ObjectAlreadyExists:
                        self.job.logger.warning(f"Duplicate Device discovered, {dev}")
                    except ValueError as exc:
                        self.job.logger.error(
                            f"Pydantic validation failed for device '{dev.get('name')}' "
                            f"(serial={dev.get('serial_number')}). "
                            f"Fields passed: {list(dev.keys())}. "
                            f"Full error: {exc}"
                        )

        # --- DIAGNOSTIC START ---
        try:
            loaded_hostnames = {d.name for d in self.get_all(self.device)}
            missing_devs = []
            for d in self.client.devices.all:
                host_val = getattr(d, 'hostname', None)
                if host_val not in loaded_hostnames and str(host_val) + "-1" not in loaded_hostnames:
                    missing_devs.append(d)
                    
            if missing_devs:
                self.job.logger.warning(f"DIAGNOSTIC: DiffSync dropped {len(missing_devs)} IPFabric devices before Nautobot sync comparison!")
                
                # Check why the first 10 failed
                for m in missing_devs[:10]:
                    reason = "Unknown"
                    host_val = getattr(m, 'hostname', 'UnknownHost')
                    site_val = getattr(m, 'site', None)
                    if not site_val:
                        reason = "Device has NO SITE assigned natively in IPFabric!"
                    elif not any(loc.name == site_val for loc in self.get_all(self.location)):
                        reason = f"Device site '{site_val}' was filtered out or missing from parsed Locations!"
                    elif not getattr(m, 'sn', None):
                        reason = "Device natively has NO SERIAL NUMBER in IPFabric!"
                        
                    self.job.logger.warning(f"Dropped Device '{host_val}' (Family: {getattr(m, 'family', None)}, Site: {site_val}, SDK SN: {getattr(m, 'sn', None)}): Reason -> {reason}")
        except Exception as e:
            self.job.logger.error(f"Diagnostic script failed: {e}")
        # --- DIAGNOSTIC END ---

def pseudo_management_interface(hostname, device_interfaces, device_primary_ip):
    """Return a dict for an non-existing interface for NAT management addresses."""
    if any(iface for iface in device_interfaces if iface.get("primaryIp", "") == device_primary_ip):
        return None
    return {
        "hostname": hostname,
        "intName": "pseudo_mgmt",
        "dscr": "pseudo interface for NAT IP address",
        "primaryIp": device_primary_ip,
        "type": "virtual",
        "mgmt_only": True,
    }
