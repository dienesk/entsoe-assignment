import sys
from pathlib import Path

# The Lambda deployment package puts lambda/src on sys.path (see how the
# handler modules import each other with bare names like `from dates import
# ...`), so tests need the same layout rather than a package-relative import.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
