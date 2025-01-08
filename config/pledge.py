from dataclasses import dataclass
from typing import Set, Optional
import os

@dataclass
class PledgeConfig:
    addresses: Set[str]
    _instance: Optional['PledgeConfig'] = None

    @classmethod
    def from_env(cls) -> 'PledgeConfig':
        if cls._instance is None:
            pledge_addresses = set()
            if pledge_str := os.environ.get("PLEDGE_ADDRESS"):
                pledge_addresses = {addr.strip() for addr in pledge_str.split(",")}
            cls._instance = cls(addresses=pledge_addresses)
            print(f"Initialized PledgeConfig with {len(pledge_addresses)} addresses")
        return cls._instance

    def is_pledge_address(self, address: str) -> bool:
        return address in self.addresses 