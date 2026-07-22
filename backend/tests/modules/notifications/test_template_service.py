import pytest

from long_invest.modules.notifications.template_catalog import (
    GIT_TEMPLATE_REGISTRY,
    TemplateVersionNotFoundError,
)
from long_invest.modules.notifications.template_service import (
    NotificationTemplateService,
)


class Repository:
    def __init__(self) -> None:
        self.active = {}
        self.synced = False

    async def sync_templates(self, registry):
        assert registry is GIT_TEMPLATE_REGISTRY
        self.synced = True

    async def resolve_active_template(self, template_type, registry):
        version = self.active.get(template_type)
        return registry.resolve(template_type, version) if version else None

    async def read_template_version(self, template_type, version):
        try:
            return GIT_TEMPLATE_REGISTRY.resolve(template_type, version)
        except TemplateVersionNotFoundError:
            return None

    async def activate_template(self, template_type, version):
        selected = await self.read_template_version(template_type, version)
        if selected is None:
            return None
        self.active[template_type] = version
        return object()


@pytest.mark.anyio
async def test_activate_syncs_git_versions_and_changes_active_pointer() -> None:
    repository = Repository()
    result = await NotificationTemplateService(repository).activate(
        "notification.test", "v1"
    )

    assert repository.synced is True
    assert repository.active["notification.test"] == "v1"
    assert result.definition.version == "v1"
    assert result.changed is True


@pytest.mark.anyio
async def test_activate_rejects_unknown_immutable_version() -> None:
    repository = Repository()

    with pytest.raises(TemplateVersionNotFoundError):
        await NotificationTemplateService(repository).activate(
            "notification.test", "missing"
        )
