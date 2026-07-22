from dataclasses import dataclass

from long_invest.modules.notifications.repository import NotificationRepository
from long_invest.modules.notifications.template_catalog import (
    GIT_TEMPLATE_REGISTRY,
    TemplateRegistry,
    TemplateVersionNotFoundError,
)
from long_invest.modules.notifications.templates import TemplateDefinition


@dataclass(frozen=True, slots=True)
class TemplateActivationResult:
    definition: TemplateDefinition
    changed: bool


class NotificationTemplateService:
    def __init__(
        self,
        repository: NotificationRepository,
        registry: TemplateRegistry = GIT_TEMPLATE_REGISTRY,
    ) -> None:
        self._repository = repository
        self._registry = registry

    async def sync(self) -> None:
        await self._repository.sync_templates(self._registry)

    async def activate(
        self,
        template_type: str,
        version: str,
    ) -> TemplateActivationResult:
        await self.sync()
        current = await self._repository.resolve_active_template(
            template_type, self._registry
        )
        selected = await self._repository.read_template_version(template_type, version)
        if selected is None:
            raise TemplateVersionNotFoundError(
                f"notification template version not found: {template_type}@{version}"
            )
        activation = await self._repository.activate_template(template_type, version)
        if activation is None:
            raise TemplateVersionNotFoundError(
                f"notification template version not found: {template_type}@{version}"
            )
        return TemplateActivationResult(
            definition=selected,
            changed=current is None or current.version != version,
        )
