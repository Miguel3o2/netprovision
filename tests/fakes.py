"""Test doubles for the device transport."""

from __future__ import annotations

from typing import Optional


class FakeConnection:
    """Records what would have been sent to a switch."""

    def __init__(self, fail_on: Optional[str] = None) -> None:
        self.fail_on = fail_on
        self.config_blocks: list[list[str]] = []
        self.commands: list[str] = []
        self.saved = 0
        self.disconnected = False

    def send_config_set(self, lines, **kwargs):
        block = list(lines)
        self.config_blocks.append(block)
        joined = "\n".join(block)
        if self.fail_on and self.fail_on in joined:
            raise RuntimeError(f"device rejected: {self.fail_on}")
        return f"ok\n{joined}"

    def send_command(self, command, expect_string=None, **kwargs):
        self.commands.append(command)
        if command.strip() in ("write memory", "copy running-config startup-config"):
            self.saved += 1
            return "Building configuration...\n[OK]"
        if self.fail_on and self.fail_on in command:
            raise RuntimeError(f"device rejected: {self.fail_on}")
        return "ok"

    def enable(self):
        self.commands.append("enable")

    def disconnect(self):
        self.disconnected = True


class FakeTransport:
    """Stands in for `transport.connect`, keyed by hostname."""

    def __init__(self) -> None:
        self.connections: dict[str, FakeConnection] = {}
        self.created: list[tuple[str, FakeConnection]] = []
        self.fail_on: dict[str, Optional[str]] = {}
        self.connect_error: Optional[Exception] = None

    def connect(self, creds):
        if self.connect_error is not None:
            raise self.connect_error
        conn = FakeConnection(self.fail_on.get(creds.hostname))
        self.connections[creds.hostname] = conn
        self.created.append((creds.hostname, conn))
        return conn

    def blocks_for(self, host: str) -> list[str]:
        return [
            "\n".join(block)
            for name, conn in self.created
            if name == host
            for block in conn.config_blocks
        ]

    def commands_for(self, host: str) -> list[str]:
        return [
            command
            for name, conn in self.created
            if name == host
            for command in conn.commands
        ]
