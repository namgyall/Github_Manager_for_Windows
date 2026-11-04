from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python make_icon.py OUTPUT.ico", file=sys.stderr)
        return 2
    source = Path(__file__).resolve().parent / "Resources" / "GithubManagerIcon.png"
    image = QImage(str(source))
    if image.isNull():
        print(f"Could not load icon artwork: {source}", file=sys.stderr)
        return 1
    image = image.scaled(
        256,
        256,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    png = QByteArray()
    buffer = QBuffer(png)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
        print("Could not encode the Windows application icon.", file=sys.stderr)
        return 1
    buffer.close()
    payload = bytes(png)
    directory = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(payload), 22)
    Path(sys.argv[1]).write_bytes(directory + entry + payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
