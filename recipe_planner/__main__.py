import argparse
import os
import socket
import threading
import webbrowser

from . import notify
from .app import create_app

# Not 5000: on macOS the AirPlay Receiver listens there and answers with a 403 page.
DEFAULT_PORT = 5055


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packets sent; just picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return "your-computer-ip"


def port_in_use(port: int) -> bool:
    """True if something already answers on this port (e.g. AirPlay on macOS)."""
    for host in ("127.0.0.1", "::1"):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def free_port(start: int, tries: int = 20) -> int:
    for port in range(start, start + tries):
        if not port_in_use(port):
            return port
    raise SystemExit(f"No free port found between {start} and {start + tries - 1}. Try --port.")


def main():
    p = argparse.ArgumentParser(description="Run the recipe planner.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    p.add_argument("--lan", action="store_true",
                   help="serve on your home network so your phone can use it")
    p.add_argument("--open", action="store_true", help="open the app in your browser")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    host = "0.0.0.0" if args.lan else args.host
    port = free_port(args.port)
    if port != args.port:
        print(f"Port {args.port} is busy (on a Mac that's often AirPlay); using {port} instead.")
    url = f"http://127.0.0.1:{port}"
    print(f"Meal Planner → {url}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"On your phone (same Wi-Fi), open: http://{_lan_ip()}:{port}")
        if not os.environ.get("RECIPE_PLANNER_PASSWORD"):
            print("Tip: set RECIPE_PLANNER_PASSWORD so others on your network can't open it.")
    if args.open:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    app = create_app()
    notify.start_scheduler(app.config["DB_FILE"])      # phone reminders, if you've set a topic in Settings
    app.run(host=host, port=port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
