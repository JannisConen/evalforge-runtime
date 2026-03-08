"""EvalForge Runtime — execution engine for generated applications."""

__version__ = "0.1.0"


def main() -> None:
    """CLI entry point: evalforge-runtime start --config <path>"""
    import argparse

    from evalforge_runtime.config import load_config
    from evalforge_runtime.server import create_app

    parser = argparse.ArgumentParser(
        prog="evalforge-runtime",
        description="EvalForge Runtime Server",
    )
    subparsers = parser.add_subparsers(dest="command")

    start_parser = subparsers.add_parser("start", help="Start the runtime server")
    start_parser.add_argument(
        "--config", default="evalforge.config.yaml", help="Path to config YAML"
    )
    start_parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    start_parser.add_argument("--port", type=int, default=8000, help="Bind port")

    args = parser.parse_args()

    config_path = getattr(args, "config", "evalforge.config.yaml")
    host = getattr(args, "host", "0.0.0.0")
    port = getattr(args, "port", 8000)

    config = load_config(config_path)
    app = create_app(config)

    import uvicorn

    uvicorn.run(app, host=host, port=port)
