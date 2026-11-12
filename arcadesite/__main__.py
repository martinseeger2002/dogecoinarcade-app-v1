"""python -m arcadesite --home ~/.arcade-site [--host 127.0.0.1] [--port 8460]"""
import argparse
import os
import sys


def main() -> None:
    ap = argparse.ArgumentParser(prog="arcadesite")
    ap.add_argument("--home", default="~/.arcade-site")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8460)
    a = ap.parse_args()
    src = os.environ.get("ARCADE_SRC")
    if src and src not in sys.path:
        sys.path.insert(0, src)
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(a.home), host=a.host, port=a.port, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                log_level="info")


if __name__ == "__main__":
    main()
