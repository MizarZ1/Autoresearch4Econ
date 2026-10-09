"""The environment probe validates action shape, independently of API access."""

import pytest
from pydantic import ValidationError

from scripts.probe_model import ProbeAction


@pytest.mark.parametrize(
    "response",
    ['{"action":"execute","message":"ok"}', '{"action":"finish","extra":1}', "not json"],
)
def test_probe_rejects_invalid_actions(response: str) -> None:
    with pytest.raises(ValidationError):
        ProbeAction.model_validate_json(response)
