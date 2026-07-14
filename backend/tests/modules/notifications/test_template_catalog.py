import importlib
import importlib.util

import pytest


def load_catalog():
    module_name = "long_invest.modules.notifications.template_catalog"
    assert importlib.util.find_spec(module_name) is not None, (
        "Git-managed notification template catalog is not implemented"
    )
    return importlib.import_module(module_name)


def test_git_template_catalog_contains_v31_minimum_template_set() -> None:
    catalog = load_catalog()

    assert catalog.GIT_TEMPLATE_REGISTRY.template_types() >= {
        "signal.low",
        "signal.low_cleared",
        "signal.high",
        "signal.high_cleared",
        "system.error",
        "system.critical",
        "system.recovered",
        "daily_data.incomplete",
        "target.review_required",
        "notification.test",
    }


def test_template_catalog_resolves_exact_immutable_version() -> None:
    catalog = load_catalog()

    definition = catalog.GIT_TEMPLATE_REGISTRY.resolve("notification.test", "v1")

    assert definition.template_type == "notification.test"
    assert definition.version == "v1"
    with pytest.raises(TypeError):
        catalog.GIT_TEMPLATE_REGISTRY.definitions[("notification.test", "v1")] = (
            definition
        )


def test_template_catalog_rejects_unknown_version() -> None:
    catalog = load_catalog()

    with pytest.raises(catalog.TemplateVersionNotFoundError) as exc_info:
        catalog.GIT_TEMPLATE_REGISTRY.resolve("notification.test", "missing")

    assert exc_info.value.code == "NOTIFICATION_TEMPLATE_VERSION_NOT_FOUND"
