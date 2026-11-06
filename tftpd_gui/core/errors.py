class PortBusy(OSError):
    """Raised when a server cannot bind its port because it is already in use."""

    def __init__(self, port: int):
        super().__init__(port)
        self.port = port


class DhcpConfigError(Exception):
    """A DHCP configuration that must not be started.

    ``code`` is one of: no_ipv4, mask_invalid, pool_reversed,
    pool_outside_subnet, pool_contains_server, pool_contains_gateway, gateway_outside_subnet,
    dns_invalid.
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
