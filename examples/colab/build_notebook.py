"""Build ml_sentry_colab_walkthrough.ipynb from colab_walkthrough.py + ml_sentry_client.py.

The notebook embeds the client via %%writefile so it is self-contained in Colab.
Run:  python examples/colab/build_notebook.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
NOTEBOOK = HERE / "ml_sentry_colab_walkthrough.ipynb"


def cells_from_percent_script(text: str) -> list[dict[str, object]]:
    cells: list[dict[str, object]] = []
    for chunk in re.split(r"^# %%", text, flags=re.MULTILINE)[1:]:
        header, _, body = chunk.partition("\n")
        if header.strip() == "[markdown]":
            source = "\n".join(
                line[2:] if line.startswith("# ") else line.lstrip("#")
                for line in body.strip().splitlines()
            )
            cells.append({"cell_type": "markdown", "metadata": {}, "source": source})
        else:
            cells.append(
                {
                    "cell_type": "code",
                    "metadata": {},
                    "execution_count": None,
                    "outputs": [],
                    "source": body.strip(),
                }
            )
    return cells


def build() -> dict[str, object]:
    client = (HERE / "ml_sentry_client.py").read_text(encoding="utf-8")
    cells = cells_from_percent_script((HERE / "colab_walkthrough.py").read_text(encoding="utf-8"))
    writefile = {
        "cell_type": "code",
        "metadata": {},
        "execution_count": None,
        "outputs": [],
        "source": "%%writefile ml_sentry_client.py\n" + client,
    }
    cells.insert(1, writefile)
    for cell in cells:
        lines = str(cell["source"]).splitlines(keepends=True)
        cell["source"] = lines
    return {
        "cells": cells,
        "metadata": {
            "colab": {"provenance": []},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 0,
    }


if __name__ == "__main__":
    NOTEBOOK.write_text(json.dumps(build(), indent=1) + "\n", encoding="utf-8")
    print(f"wrote {NOTEBOOK}")
