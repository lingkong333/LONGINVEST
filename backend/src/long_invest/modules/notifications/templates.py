import html as html_module
import re
from dataclasses import dataclass
from typing import Any

from long_invest.modules.notifications.security import validate_notification_payload

_VARIABLE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


class TemplateRenderError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        missing_fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.missing_fields = missing_fields


@dataclass(frozen=True, slots=True)
class TemplateDefinition:
    template_type: str
    version: str
    text: str
    subject: str | None = None
    html: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedTemplate:
    template_type: str
    version: str
    subject: str | None
    text: str
    html: str | None


def _fields(value: str | None) -> set[str]:
    if value is None:
        return set()
    without_variables = _VARIABLE.sub("", value)
    if any(marker in without_variables for marker in ("{{", "}}", "{%", "%}")):
        raise TemplateRenderError(
            code="NOTIFICATION_TEMPLATE_UNSAFE",
            message="template contains an unsupported expression",
        )
    return set(_VARIABLE.findall(value))


def _render(
    value: str | None,
    variables: dict[str, Any],
    *,
    escape: bool,
) -> str | None:
    if value is None:
        return None

    def replace(match: re.Match[str]) -> str:
        rendered = str(variables[match.group(1)])
        return html_module.escape(rendered, quote=True) if escape else rendered

    return _VARIABLE.sub(replace, value)


class StrictTemplateRenderer:
    def render(
        self,
        definition: TemplateDefinition,
        variables: dict[str, Any],
        *,
        test_message: bool = False,
    ) -> RenderedTemplate:
        safe_variables = validate_notification_payload(variables)
        used_fields = set().union(
            _fields(definition.subject),
            _fields(definition.text),
            _fields(definition.html),
        )
        missing = tuple(sorted(used_fields - safe_variables.keys()))
        if missing:
            raise TemplateRenderError(
                code="NOTIFICATION_TEMPLATE_MISSING_FIELD",
                message="template variables are missing",
                missing_fields=missing,
            )

        subject = _render(definition.subject, safe_variables, escape=False)
        text = _render(definition.text, safe_variables, escape=False)
        rendered_html = _render(definition.html, safe_variables, escape=True)
        if subject is not None and ("\r" in subject or "\n" in subject):
            raise TemplateRenderError(
                code="NOTIFICATION_TEMPLATE_UNSAFE",
                message="rendered subject cannot contain a newline",
            )
        if test_message:
            subject = f"[TEST] {subject or definition.template_type}"
            text = f"[TEST MESSAGE] {text}"
            if rendered_html is not None:
                rendered_html = f"<p><strong>TEST MESSAGE</strong></p>{rendered_html}"

        return RenderedTemplate(
            definition.template_type,
            definition.version,
            subject,
            text or "",
            rendered_html,
        )
