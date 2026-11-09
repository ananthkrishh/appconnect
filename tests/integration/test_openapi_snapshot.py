"""The committed OpenAPI documents must match what the code generates.

CI's breaking-change gate diffs the committed ``openapi/v2.yaml`` from the base
branch against this branch's, so a stale snapshot would let changes slip by.
"""

from io import StringIO
from pathlib import Path

import pytest
import yaml
from django.core.management import call_command

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_committed_schema_is_current(version):
    out = StringIO()
    call_command("spectacular", "--api-version", version, "--validate", stdout=out)
    generated = yaml.safe_load(out.getvalue())
    committed = yaml.safe_load((ROOT / "openapi" / f"{version}.yaml").read_text())
    assert generated == committed, f"Run `make openapi` to refresh openapi/{version}.yaml"
