"""
run.py - Convenience launcher for the F1 Live Race Insight Architecture.

Usage:
    python run.py [--port PORT] [--host HOST]
"""

import argparse
import subprocess
import sys
import os

def main():
    parser = argparse.ArgumentParser(description="Launch the F1 Insight backend server.")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Enable hot-reload for development")
    args = parser.parse_args()

    print("=" * 60)
    print("  🏎️   F1 Live Race Insight Architecture")
    print("=" * 60)
    print(f"  Host   : {args.host}")
    print(f"  Port   : {args.port}")
    print(f"  Reload : {args.reload}")
    print(f"  URL    : http://localhost:{args.port}")
    print(f"  Health : http://localhost:{args.port}/health")
    print(f"  Grid   : http://localhost:{args.port}/grid")
    print(f"  WS     : ws://localhost:{args.port}/ws")
    print("=" * 60)
    print("  Open frontend/index.html in your browser to view the dashboard.")
    print("=" * 60)

    cmd = [
        sys.executable, "-m", "uvicorn",
        "backend.app:app",
        "--host", args.host,
        "--port", str(args.port),
        "--log-level", "info",
    ]
    if args.reload:
        cmd.append("--reload")

    try:
        subprocess.run(cmd, cwd=os.path.dirname(os.path.abspath(__file__)), check=True)
    except KeyboardInterrupt:
        print("\n🛑  Server stopped by user.")


if __name__ == "__main__":
    main()
