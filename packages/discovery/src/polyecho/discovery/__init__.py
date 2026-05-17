"""polyecho.discovery — Discovery-worker service.

Discovers, scores, and tracks leader candidates on Polymarket (PRD §5.2, §5.3).
This process holds no private keys and opens no network ports.
"""

from polyecho.discovery.worker import DiscoveryWorker

__all__ = ["DiscoveryWorker"]
