"""Examples and templates stay valid and in sync."""

import json
import sys

from typer.testing import CliRunner

from ml_sentry.cli import app as cli
from ml_sentry.policies.loader import load_policy
from ml_sentry.settings import parse_reviewers
from tests.conftest import ROOT

COLAB = ROOT / "examples" / "colab"


def test_policy_templates_are_valid() -> None:
    for name in ("governance.example.yaml", "my-model.example.yaml"):
        load_policy(ROOT / "configs" / name)


def test_notebook_is_built_from_current_sources() -> None:
    sys.path.insert(0, str(COLAB))
    import build_notebook

    committed = json.loads((COLAB / "ml_sentry_colab_walkthrough.ipynb").read_text("utf-8"))
    assert committed == json.loads(json.dumps(build_notebook.build())), (
        "run: python examples/colab/build_notebook.py"
    )
    writefile = "".join(committed["cells"][1]["source"])
    assert writefile.startswith("%%writefile ml_sentry_client.py")


def test_notebook_never_asks_for_a_reviewer_token() -> None:
    text = (COLAB / "colab_walkthrough.py").read_text("utf-8")
    # Only the service and runner tokens are read; approvals happen in the UI.
    assert "ML_SENTRY_REVIEWER" not in text
    assert "/decision" not in text


def test_add_reviewer_prints_a_valid_hash_entry() -> None:
    result = CliRunner().invoke(cli, ["add-reviewer", "carol"])
    assert result.exit_code == 0
    entry = result.output.strip().splitlines()[-1].strip()
    assert parse_reviewers(entry).keys() == {"carol"}
    bad = CliRunner().invoke(cli, ["add-reviewer", "Bad Name"])
    assert bad.exit_code != 0
