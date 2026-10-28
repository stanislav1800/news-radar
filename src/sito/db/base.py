from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Enum, Identity, MetaData, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",                         # индекс
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",         # unique
    "ck": "ck_%(table_name)s_%(constraint_name)s",         # check
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",                             # первичный ключ
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {str: Text, datetime: DateTime(timezone=True)}
    

class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True, sort_order=-100)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), sort_order=100)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now(), sort_order=100)

def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )