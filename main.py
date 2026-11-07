#!/usr/bin/env python3
"""사용법:  python main.py commit   |   python main.py pr   (자세히: python main.py -h)"""
import sys

if sys.version_info < (3, 9):
    sys.exit("[ERROR] Python 3.9 이상이 필요합니다 (권장 3.10+). 현재: " + sys.version.split()[0])

from gitgen.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
