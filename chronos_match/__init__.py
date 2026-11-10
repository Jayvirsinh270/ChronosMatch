"""
ChronosMatch is now FluxCore.
This backward-compatibility shim redirects all legacy `chronos_match` imports to `fluxcore`.
"""
import sys
import fluxcore

# Forward submodule imports dynamically
sys.modules["chronos_match"] = fluxcore
