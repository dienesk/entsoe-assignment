import sys
import types
from pathlib import Path

# The Lambda deployment package puts lambda/src on sys.path (see how the
# handler modules import each other with bare names like `from dates import
# ...`), so tests need the same layout rather than a package-relative import.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# boto3 ships with the Lambda runtime rather than with this repo, and
# handler.py creates its clients at import time. Stubbed here rather than in
# one test module so any test file can import the handler on its own.
if "boto3" not in sys.modules:
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service_name: None
    sys.modules["boto3"] = fake_boto3
