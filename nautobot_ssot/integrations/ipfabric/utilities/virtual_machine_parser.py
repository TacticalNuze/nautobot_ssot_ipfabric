import re
from typing import Dict, Any, List

def parse_virtual_machine_name(full_name: str, serial_number: str) -> tuple:
    """Strip the last segment or trailing slash from names, and remove the first slash from serial numbers."""
    if full_name and ".gt.ferlan.it" in full_name:
        full_name = full_name.replace(".gt.ferlan.it", "")

    def process_string(s: str) -> str:
        if not s:
            return s
            
        # "if no slash is found return original string"
        if '/' not in s:
            return s
            
        # "for trailing slashes remove the trailing slash and return the rest of the string"
        if s.endswith('/'):
            return s.rstrip('/')
            
        # Reformat the names and take the suffix before the last '/'
        return s.rsplit('/', 1)[0]

    final_name = process_string(full_name)
    final_serial = serial_number.split("/",1)[0] if serial_number else ""
    return final_name, final_serial


