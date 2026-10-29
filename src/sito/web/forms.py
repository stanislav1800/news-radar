"""Формы по описанию Pydantic-модели: поля, подписи, ограничения и ошибки на русском.

Так добавление нового типа источников не требует новых шаблонов: форма строится по модели.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import annotated_types
from pydantic import AnyUrl, BaseModel, SecretStr, ValidationError
from pydantic_core import PydanticUndefined


@dataclass(frozen=True, slots=True)
class FormField:
    name: str
    label: str
    help: str | None
    input_type: str
    required: bool
    value: str
    error: str | None
    min: int | float | None = None
    max: int | float | None = None
    maxlength: int | None = None


def _input_type(annotation: Any) -> str:
    if annotation is bool:
        return "checkbox"
    if annotation in (int, float):
        return "number"
    if annotation is SecretStr:
        return "password"
    if isinstance(annotation, type) and issubclass(annotation, AnyUrl):
        return "url"
    return "text"


def form_fields(
    model: type[BaseModel], values: Mapping[str, str] | None = None, errors: Mapping[str, str] | None = None
) -> list[FormField]:
    values, errors = values or {}, errors or {}
    fields = []
    for name, info in model.model_fields.items():
        default = "" if info.default is PydanticUndefined or info.default is None else str(info.default)
        bounds: dict[str, Any] = {}
        for meta in info.metadata:
            if isinstance(meta, annotated_types.Ge):
                bounds["min"] = meta.ge
            elif isinstance(meta, annotated_types.Le):
                bounds["max"] = meta.le
            elif isinstance(meta, annotated_types.MaxLen):
                bounds["maxlength"] = meta.max_length
        fields.append(FormField(
            name=name,
            label=info.title or name,
            help=info.description,
            input_type=_input_type(info.annotation),
            required=info.is_required(),
            value=values.get(name, default),
            error=errors.get(name),
            **bounds,
        ))
    return fields


_MESSAGES = {
    "missing": "Обязательное поле",
    "string_too_short": "Обязательное поле",
    "string_too_long": "Не длиннее {max_length} символов",
    "url_parsing": "Нужна ссылка вида https://example.ru/rss",
    "url_scheme": "Ссылка должна начинаться с http:// или https://",
    "int_parsing": "Нужно целое число",
    "greater_than_equal": "Не меньше {ge}",
    "less_than_equal": "Не больше {le}",
}


def validation_errors(exc: ValidationError) -> dict[str, str]:
    """Ошибки проверки формы: поле → понятное сообщение."""
    errors: dict[str, str] = {}
    for error in exc.errors():
        field = str(error["loc"][0]) if error["loc"] else "__all__"
        template = _MESSAGES.get(error["type"])
        errors.setdefault(field, template.format(**error.get("ctx", {})) if template else error["msg"])
    return errors
