from __future__ import annotations

import sys

from main import main


def func(question: str) -> int:
    return main(["demo", question])


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]).strip() or "hello"
    raise SystemExit(func(question))
