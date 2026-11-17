import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ai_rdp.backend import DemoBackend
from ai_rdp.service import Controller, LocalServer, write_runtime


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_handshake_images_input_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(DemoBackend(), demo=True)
            controller.state = "connected"
            controller.frames.publish(controller.backend.capture())
            server = LocalServer(("127.0.0.1", 0), controller, "control", "watch")
            write_runtime(Path(directory), server.server_port, "control", "watch", controller.frames.session_id)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "ai_rdp", "mcp", "--state-dir", directory], cwd=str(Path(__file__).resolve().parent.parent))
            try:
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual({t.name for t in tools.tools}, {"rdp_status", "rdp_observe", "rdp_act", "rdp_stop"})
                        result = await session.call_tool("rdp_observe", {"mode": "full"})
                        self.assertFalse(result.isError)
                        self.assertTrue(any(c.type == "image" for c in result.content))
                        metadata = json.loads(result.content[0].text)
                        fid = metadata["frame_id"]
                        result = await session.call_tool("rdp_act", {"frame_id": fid, "actions": [{"type": "text", "text": "MCP Unicode 中文"}], "observe_after": False})
                        self.assertFalse(result.isError)
                        self.assertEqual(controller.backend.value, "MCP Unicode 中文")
                        result = await session.call_tool("rdp_stop", {})
                        self.assertFalse(result.isError)
                        result = await session.call_tool("rdp_act", {"frame_id": fid, "actions": [{"type": "key", "keys": ["enter"]}]})
                        self.assertTrue(result.isError)
            finally:
                server.shutdown()
                server.server_close()
