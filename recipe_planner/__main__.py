import argparse
import os
import socket

from .app import create_app


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packets sent; just picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return "your-computer-ip"


def main():
    p = argparse.ArgumentParser(description="Run the recipe planner.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--lan", action="store_true",
                   help="serve on your home network so your phone can use it")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    host = "0.0.0.0" if args.lan else args.host
    if host not in ("127.0.0.1", "localhost"):
        print(f"On your phone (same Wi-Fi), open: http://{_lan_ip()}:{args.port}")
        if not os.environ.get("RECIPE_PLANNER_PASSWORD"):
            print("Tip: set RECIPE_PLANNER_PASSWORD so others on your network can't open it.")
    create_app().run(host=host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
