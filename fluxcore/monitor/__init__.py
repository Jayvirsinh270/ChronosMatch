"""
FluxCore: Real-Time Performance Monitoring, Metrics & Terminal UI.
"""
from fluxcore.monitor.metrics import LatencyProfiler, LatencyStats
from fluxcore.monitor.whale_detector import WhaleDetector, WhaleAlert
from fluxcore.monitor.dashboard import TerminalDashboard

__all__ = [
    "LatencyProfiler",
    "LatencyStats",
    "WhaleDetector",
    "WhaleAlert",
    "TerminalDashboard",
]
