"""Serialize BCC compilation across the clause and network collectors.

libbcc may change the process working directory while resolving kernel
headers. Concurrent BPF constructors in different collectors can otherwise
fail intermittently with missing headers despite a valid kernel build tree.
Only construction is serialized; collection remains concurrent.
"""
from threading import RLock

BCC_COMPILE_LOCK = RLock()
