import re
from typing import Dict, Any, List
def parse_virtual_machine_name(full_name: str, serial_number: str) -> tuple:
    final_name = full_name.split('/')[0] if full_name else full_name
    final_serial = serial_number.split('/')[0] if serial_number else serial_number
    return final_name, final_serial


