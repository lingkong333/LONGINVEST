from collections.abc import Iterable, Mapping
from types import MappingProxyType

from long_invest.modules.notifications.templates import TemplateDefinition


class TemplateVersionNotFoundError(LookupError):
    code = "NOTIFICATION_TEMPLATE_VERSION_NOT_FOUND"


class TemplateRegistry:
    def __init__(self, definitions: Iterable[TemplateDefinition]) -> None:
        indexed: dict[tuple[str, str], TemplateDefinition] = {}
        for definition in definitions:
            key = (definition.template_type, definition.version)
            if key in indexed:
                raise ValueError(f"duplicate notification template version: {key}")
            indexed[key] = definition
        self._definitions = MappingProxyType(indexed)

    @property
    def definitions(self) -> Mapping[tuple[str, str], TemplateDefinition]:
        return self._definitions

    def template_types(self) -> set[str]:
        return {template_type for template_type, _version in self._definitions}

    def resolve(self, template_type: str, version: str) -> TemplateDefinition:
        try:
            return self._definitions[(template_type, version)]
        except KeyError as exc:
            raise TemplateVersionNotFoundError(
                f"notification template version not found: {template_type}@{version}"
            ) from exc


def _signal_template(template_type: str, title: str) -> TemplateDefinition:
    return TemplateDefinition(
        template_type=template_type,
        version="v1",
        subject=f"{title}: {{{{ symbol }}}} {{{{ name }}}}",
        text=(
            "{{ symbol }} {{ name }}: {{ previous_state }} -> {{ current_state }}; "
            "price={{ price }} at {{ quote_time }}; targets={{ targets }}; "
            "target={{ target_version }}/{{ target_date }} stale={{ target_stale }}; "
            "holding={{ holding }}; reason={{ reason }}; event={{ event_id }}"
        ),
    )


GIT_TEMPLATE_REGISTRY = TemplateRegistry(
    (
        _signal_template("signal.low", "Low signal"),
        _signal_template("signal.low_cleared", "Low signal cleared"),
        _signal_template("signal.high", "High signal"),
        _signal_template("signal.high_cleared", "High signal cleared"),
        TemplateDefinition(
            "system.warning",
            "v1",
            "Warning {{ alert_type }}: {{ message }}; event={{ event_id }}",
            subject="System warning: {{ alert_type }}",
        ),
        TemplateDefinition(
            "system.error",
            "v1",
            "Error {{ alert_type }}: {{ message }}; event={{ event_id }}",
            subject="System error: {{ alert_type }}",
        ),
        TemplateDefinition(
            "system.critical",
            "v1",
            "Critical {{ alert_type }}: {{ message }}; event={{ event_id }}",
            subject="Critical system alert: {{ alert_type }}",
        ),
        TemplateDefinition(
            "system.recovered",
            "v1",
            "Recovered {{ alert_type }}: {{ message }}; event={{ event_id }}",
            subject="System recovered: {{ alert_type }}",
        ),
        TemplateDefinition(
            "daily_data.incomplete",
            "v1",
            "Daily data incomplete: {{ summary }}; event={{ event_id }}",
            subject="Daily data incomplete",
        ),
        TemplateDefinition(
            "target.review_required",
            "v1",
            "Target review required for {{ symbol }}: {{ summary }}; "
            "event={{ event_id }}",
            subject="Target review required: {{ symbol }}",
        ),
        TemplateDefinition(
            "notification.test",
            "v1",
            "Test message: {{ message }}; event={{ event_id }}",
            subject="Notification channel test",
        ),
    )
)
