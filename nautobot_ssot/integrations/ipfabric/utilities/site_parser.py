import re
from typing import Dict, Any, List
import logging
logger = logging.getLogger("nautobot.jobs")
def parse_site_hierarchy(sites: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Parses IP Fabric site names to determine location type and parent-child hierarchy.
    
    It expects prefixes like "01- DATACENTER" or "1.1 CED" or "1.1.1 CED" to determine hierarchy:
    - location_type: derived from the suffix of the top-most parent (e.g. "DATACENTER")
    - parent_name: derived by resolving the prefix minus its last depth segment
    
    Returns:
        A dictionary mapping 'siteName' to a dict with 'parent_name' and 'location_type'.
    """
    parsed_sites = {}
    prefix_map = {}
    
    # regex matches e.g. "01- NAME" or "1.1 NAME" or "1.1.1 NAME"
    prefix_pattern = re.compile(r"^(\d+(?:\.\d+)*)(?:[-\s]+)(.*)")
    
    # Pass 1: Extract prefix tuples and map them
    try: 
        for site in sites:
            site_name = site.get("siteName", "")
            if not site_name:
                continue
                
            match = prefix_pattern.match(site_name)
            if match:
                prefix_str = match.group(1)
                # convert "01" or "02" to int for consistent lookup tuple
                prefix_tuple = tuple(int(x) for x in prefix_str.split("."))
                suffix_name = match.group(2).strip()
                
                prefix_map[prefix_tuple] = {
                    "site_name": site_name,
                    "suffix": suffix_name,
                }
                parsed_sites[site_name] = {
                    "parent_name": None,
                    "location_type": "Site", # fallback
                    "prefix_tuple": prefix_tuple
                }
            else:
                # Fallback for sites that don't match the convention
                parsed_sites[site_name] = {
                    "parent_name": None,
                    "location_type": "Site",
                    "prefix_tuple": None
                }
    except Exception as e:
        logger.error(f"Error while parsing the sites. Error message {e}")
        
    try:
        # Pass 2: Resolve location types and parent sites
        for site_name, data in parsed_sites.items():
            prefix_tuple = data.get("prefix_tuple")
            if not prefix_tuple:
                continue
                
            # Location type is determined by the suffix of the root prefix (e.g. `(1,)`)
            root_prefix = prefix_tuple[0:1]
            root_info = prefix_map.get(root_prefix)
            if root_info:
                data["location_type"] = root_info["suffix"]
                
            # Parent name is determined by dropping the last element of the prefix tuple
            if len(prefix_tuple) > 1:
                parent_prefix = prefix_tuple[:-1]
                parent_info = prefix_map.get(parent_prefix)
                if parent_info:
                    data["parent_name"] = parent_info["site_name"]
                    
        # Pass 3: Preserve existing Nautobot location types
        try:
            from nautobot.dcim.models import Location as NautobotLocation
            for site_name, data in parsed_sites.items():
                existing_loc = NautobotLocation.objects.filter(name=site_name).first()
                if existing_loc and existing_loc.location_type:
                    data["location_type"] = existing_loc.location_type
        except Exception as e:
            logger.warning(f"Error checking Nautobot for existing locations: {e}")
            
        return parsed_sites
    except Exception as e:
        logger.error(f"Error while solving the location types and parent locations. Error message {e}")
