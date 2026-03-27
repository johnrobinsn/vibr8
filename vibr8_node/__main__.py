"""vibr8-node entry point — run with: uv run python -m vibr8_node"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vibr8-node")

CONFIG_DIR = Path.home() / ".vibr8-node"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config(config_path: Path | None = None) -> dict:
    path = config_path or CONFIG_FILE
    if path.exists():
        return json.loads(path.read_text())
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="vibr8 remote node agent")
    parser.add_argument("--hub", help="Hub URL (e.g., wss://vibr8.ringzero.ai)")
    parser.add_argument("--api-key", help="API key for hub authentication")
    parser.add_argument("--name", help="Display name for this node")
    parser.add_argument("--config", type=Path, help="Config file path")
    parser.add_argument("--port", type=int, help="Local port for CLI/Ring0 (default: 3457)")
    parser.add_argument("--work-dir", help="Working directory for sessions")
    args = parser.parse_args()

    config = load_config(args.config)

    # CLI args override config file
    hub_url = args.hub or config.get("hub_url", "")
    api_key = args.api_key or config.get("api_key", "")
    name = args.name or config.get("name", "")
    port = args.port or config.get("port", 3457)
    work_dir = args.work_dir or config.get("work_dir", "")

    if not hub_url:
        print("Error: --hub URL required (or set hub_url in config)", file=sys.stderr)
        sys.exit(1)
    if not api_key:
        print("Error: --api-key required (or set api_key in config)", file=sys.stderr)
        sys.exit(1)
    if not name:
        import socket
        name = socket.gethostname()
        logger.info("No --name specified, using hostname: %s", name)

    from vibr8_node.node_agent import NodeAgent

    agent = NodeAgent(
        hub_url=hub_url,
        api_key=api_key,
        name=name,
        port=port,
        work_dir=work_dir,
        ring0_config=config.get("ring0", {}),
    )

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main()
