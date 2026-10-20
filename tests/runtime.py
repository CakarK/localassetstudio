"""Local test fixtures avoid OneDrive's recursive cleanup stalls."""
import tempfile
from pathlib import Path
TEST_ROOT=(Path(tempfile.gettempdir())/'Game3DCreatorTests').resolve()
TEST_ROOT.mkdir(parents=True,exist_ok=True)
