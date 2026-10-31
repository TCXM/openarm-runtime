"""Local integration shell; importing it starts no service or hardware."""
from .config import PlatformConfig, load_config

__all__ = ['PlatformConfig', 'load_config']
