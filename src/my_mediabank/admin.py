"""Local administration; no public endpoint can mint an owner invitation."""
import argparse

from .access import AccessStore
from .config import Settings


def main():
    parser = argparse.ArgumentParser(description="My MediaBank owner access")
    sub = parser.add_subparsers(dest="command", required=True)
    invite = sub.add_parser("invite", help="Create a private single-use owner code")
    invite.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "invite":
        print(AccessStore(settings.db_path).invite(ttl_seconds=args.hours * 3600))


if __name__ == "__main__":
    main()
