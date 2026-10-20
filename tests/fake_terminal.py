import asyncio


class FakeTerminalSession:
    def __init__(self, pane_id, cols, rows):
        self.pane_id = pane_id
        self.cols = cols
        self.rows = rows
        self.inputs = []
        self.resizes = []
        self.closed = False
        self.outputs = [b"\x1b[32mTERM_READY\x1b[0m"]

    async def read(self):
        if self.outputs:
            return self.outputs.pop(0)
        await asyncio.sleep(10)
        return b""

    async def write(self, data):
        self.inputs.append(data)

    async def resize(self, cols, rows):
        self.resizes.append((cols, rows))

    async def close(self):
        self.closed = True


class FakeTerminalFactory:
    def __init__(self):
        self.sessions = []

    def __call__(self, pane_id, cols, rows):
        session = FakeTerminalSession(pane_id, cols, rows)
        self.sessions.append(session)
        return session
