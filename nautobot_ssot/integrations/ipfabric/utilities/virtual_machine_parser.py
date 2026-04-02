import re
from typing import Dict, Any, List
def parse_virtual_machine_name(full_name: str, serial_number: str) -> tuple:
    """Strip the last segment or trailing slash from names and serial numbers if a slash is present."""
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
    final_serial = process_string(serial_number)
    return final_name, final_serial


