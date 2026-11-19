"""Check integration metadata and translation keys."""

import json
import string
from pathlib import Path

from PIL import Image

from custom_components.vkusvill.const import DOMAIN, VERSION
from custom_components.vkusvill.models import Stage

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN


def test_metadata_and_translations():
    def read_json(path):
        return json.loads((COMPONENT / path).read_text(encoding="utf-8"))

    def assert_same_shape(reference, localized):
        if isinstance(reference, dict):
            assert reference.keys() == localized.keys()
            for key, value in reference.items():
                assert_same_shape(value, localized[key])
        else:
            formatter = string.Formatter()
            assert isinstance(localized, str) and localized
            assert {field for _, field, _, _ in formatter.parse(reference)} == {
                field for _, field, _, _ in formatter.parse(localized)
            }

    manifest = read_json("manifest.json")
    assert manifest["domain"] == DOMAIN
    assert manifest["version"] == VERSION
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"
    english = read_json("strings.json")
    assert english == read_json("translations/en.json")
    assert_same_shape(english, read_json("translations/ru.json"))
    assert set(english["entity"]["sensor"]["order_stage"]["state"]) == {
        stage.value for stage in Stage
    }


def test_marketplace_package():
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    hacs = json.loads((COMPONENT.parents[1] / "hacs.json").read_text(encoding="utf-8"))

    assert hacs["name"] == manifest["name"]
    assert hacs["homeassistant"] == "2026.10.4"
    assert manifest["codeowners"] == ["@inok546"]
    assert manifest["documentation"] == "https://github.com/inok546/ha-vkusvill#readme"
    assert manifest["issue_tracker"] == "https://github.com/inok546/ha-vkusvill/issues"
    for filename, size in [("icon.png", 256), ("icon@2x.png", 512)]:
        with Image.open(COMPONENT / "brand" / filename) as icon:
            assert icon.format == "PNG"
            assert icon.size == (size, size)
            assert icon.getchannel("A").getextrema() == (0, 255)
