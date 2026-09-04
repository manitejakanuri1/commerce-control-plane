"""Make the package importable without installing it first.

Running `pytest` from the repository root would otherwise fail to import
commerce_policy, because the package lives two directories down and nothing
has put it on the path. Installing it editable would also work, but a test
suite that needs an install step before it runs is one people stop running.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
