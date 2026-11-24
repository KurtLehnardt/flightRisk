"""The drone id rule, shared by the launcher and the inventory.

Drone ids name log files and processes, so they are stricter than the protocol's identifiers: no
'.' or ':' (``edge-drone:1.log`` would be a hidden NTFS stream on Windows). This module imports
nothing from the launcher, so the launcher can import the inventory without a cycle.
"""

import re

DRONE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
