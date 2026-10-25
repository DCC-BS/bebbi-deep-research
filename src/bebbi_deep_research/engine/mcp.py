"""MCP connection pool of one research agent: connect with back-off, cache, drop dead clients, remember errors."""

import threading
import time

from dcc_backend_common.logger import get_logger

from . import metrics
from .config import Config, Preset
from .tools import MCPClient

logger = get_logger(__name__)


class McpPool:
    """Connected MCP clients by server name, with the last error per server (for stats and the ledger).

    Engines call `client()` from threads (`asyncio.to_thread`); one lock per server lets several servers connect
    at the same time.
    """

    CONNECT_DEADLINE = 45  # seconds per server for the whole connect (all attempts)

    def __init__(self, cfg: Config):
        """Create an empty pool for the MCP servers of `cfg`."""
        self.cfg = cfg
        self.clients: dict[str, MCPClient] = {}
        self.errors: dict[str, str] = {}  # server -> last connect or tools/list error
        self._failed: dict[str, float] = {}
        self._lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def client(self, server: str, preset: Preset) -> MCPClient | None:
        """Return a connected MCP client for a server, or None if it is unavailable.

        Connects with 2 attempts within an overall deadline; a failure is remembered for 2 minutes only (transient
        outages happen), and callers fall back to web search meanwhile. One lock per server, so several servers
        connect concurrently when callers use threads.
        """
        with self._lock:
            lock = self._locks.setdefault(server, threading.Lock())
        with lock:
            client = self.clients.get(server)
            if client is not None:
                return client
            if time.time() - self._failed.get(server, 0) < 120:
                return None
            url = self.cfg.mcp_servers.get(server)
            if not url:
                return None
            deadline = time.monotonic() + self.CONNECT_DEADLINE
            for attempt in range(2):
                try:
                    self.clients[server] = MCPClient(url, preset.mcp.get(server), retries=3, deadline=deadline)
                    return self.clients[server]
                except Exception as e:
                    self.errors[server] = f"{type(e).__name__}: {str(e)[:200]}"
                    if time.monotonic() + 2 > deadline:
                        break
                    time.sleep(2 * (attempt + 1))
            self._failed[server] = time.time()
            metrics.inc("research_mcp_errors_total", host=server, kind="connect")
            logger.warning(
                "mcp connect failed, web search instead for 2 minutes",
                server=server,
                error=self.errors.get(server),
            )
            return None

    def drop(self, server: str, client: MCPClient, error: Exception) -> None:
        """A client whose tools/list failed is dead: forget it so the next call reconnects (after the back-off)."""
        with self._lock:
            if self.clients.get(server) is client:
                del self.clients[server]
            self._failed[server] = time.time()
            self.errors[server] = f"{type(error).__name__}: {str(error)[:200]}"
        metrics.inc("research_mcp_errors_total", host=server, kind="tools_list")
        logger.warning("mcp tools/list failed, server dropped", server=server, error=self.errors[server])
