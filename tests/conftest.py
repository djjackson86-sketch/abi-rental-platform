import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

@pytest.fixture(autouse=True)
def reset_portal_lookup_budget():
    from app.services.portal_intake import reset_rate_limits
    reset_rate_limits()
    yield
    reset_rate_limits()
