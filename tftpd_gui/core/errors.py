class PortBusy(OSError):
    """Raised when a server cannot bind its port because it is already in use."""

    def __init__(self, port: int):
        super().__init__(port)
        self.port = port
