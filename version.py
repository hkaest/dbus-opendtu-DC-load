import os
import subprocess


def _file_build_count():
    build_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_number.txt")
    try:
        with open(build_file, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
            return int(value) if value.isdigit() else 0
    except Exception:
        return 0


softwareversion = '2.0.0'
build_count = _file_build_count()
