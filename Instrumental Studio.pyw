# Double-click this on Windows to start Instrumental Studio without a console window.
import os
import runpy
import sys

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)
sys.path.insert(0, here)
sys.argv = [os.path.join(here, "app.py")] + sys.argv[1:]
runpy.run_path(os.path.join(here, "app.py"), run_name="__main__")
