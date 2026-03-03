from uuid import UUID
from sqlmodel import Field, SQLModel


class ProductAttributeLinkModel(SQLModel, table=True):
    __tablename__ = "product_attribute_link"

    attribute_id: UUID = Field(
        foreign_key="attribute_master.id",
        primary_key=True,
        ondelete="CASCADE",
    )

    product_id: UUID = Field(
        foreign_key="product_master.id",
        primary_key=True,
        ondelete="CASCADE",
    )


class ProductAttributeValueLinkModel(SQLModel, table=True):
    __tablename__ = "product_attribute_value_link"

    attribute_value_id: UUID = Field(
        foreign_key="attribute_value.id", 
        primary_key=True,
        ondelete="CASCADE",
    )

    product_id: UUID = Field(
        foreign_key="product_master.id",
        primary_key=True,
        ondelete="CASCADE",
    )