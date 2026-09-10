"""Route maintenance commands before the legacy CLI imports live storage."""

import sys


def main():
    if sys.argv[1:3] == ["circulars", "mirror"]:
        from .mirror import mirror
        mirror.main(args=sys.argv[3:], prog_name="sbpeye circulars mirror")
    else:
        from .commands import main as legacy_main
        legacy_main()


if __name__ == "__main__":
    main()
