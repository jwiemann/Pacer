"""Writes pacer.ico (exe / installer / shortcut icon): one hand-drawn frame per size."""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import tray_icon  # noqa: E402

tray_icon.write_ico(os.path.join(HERE, "pacer.ico"))
print("pacer.ico written")
