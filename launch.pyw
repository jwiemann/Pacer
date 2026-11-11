"""Start-menu entry point (kept for existing shortcuts): same as `python pacer.py launch`."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pacer
pacer.launch()
