from app.models.base import UUIDModel
from typing import Dict, List, Optional
from sqlalchemy import JSON,Column
from app.models.category import Category
from sqlmodel import  Field, Relationship
from uuid import UUID,uuid4
from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel


class AttributeValue(UUIDModel, table=True):
    __tablename__ = "attribute_value"

    value: Optional[str] = None
    uom: Optional[str] = None
    validation_value: Optional[str] = None
    validation_uom: Optional[str] = None

    attribute_id: UUID = Field(foreign_key="attribute_master.id")
    attribute: Optional["Attribute"] = Relationship(back_populates="values")

    products: List["Product"] = Relationship(
        back_populates="attribute_values",
        link_model=ProductAttributeValueLinkModel 
    )


class Attribute(UUIDModel, table=True):
    __tablename__ = "attribute_master"

    attribute_code: str = Field(unique=True, index=True)
    attribute_name: str
    category_code: Optional[str] = None
    category_path: Optional[str] = None
    description: Optional[str] = None
    applicable_categories: Optional[str] = None
    attribute_type: Optional[str] = None
    data_type: Optional[str] = None
    unit: Optional[str] = None
    filter: Optional[str] = "No"
    filter_display_name: Optional[str] = None
    usage_count: int = 0
    values_data: Dict = Field(default={}, sa_column=Column(JSON))
    variants: bool = Field(default=False)

    attribute_value_1: Optional[str] = None
    attribute_uom_1: Optional[str] = None
    attribute_value_2: Optional[str] = None
    attribute_uom_2: Optional[str] = None
    attribute_value_3: Optional[str] = None
    attribute_uom_3: Optional[str] = None
    attribute_value_4: Optional[str] = None
    attribute_uom_4: Optional[str] = None
    attribute_value_5: Optional[str] = None
    attribute_uom_5: Optional[str] = None
    attribute_value_6: Optional[str] = None
    attribute_uom_6: Optional[str] = None
    attribute_value_7: Optional[str] = None
    attribute_uom_7: Optional[str] = None
    attribute_value_8: Optional[str] = None
    attribute_uom_8: Optional[str] = None
    attribute_value_9: Optional[str] = None
    attribute_uom_9: Optional[str] = None
    attribute_value_10: Optional[str] = None
    attribute_uom_10: Optional[str] = None
    attribute_value_11: Optional[str] = None
    attribute_uom_11: Optional[str] = None
    attribute_value_12: Optional[str] = None
    attribute_uom_12: Optional[str] = None
    attribute_value_13: Optional[str] = None
    attribute_uom_13: Optional[str] = None
    attribute_value_14: Optional[str] = None
    attribute_uom_14: Optional[str] = None
    attribute_value_15: Optional[str] = None
    attribute_uom_15: Optional[str] = None
    attribute_value_16: Optional[str] = None
    attribute_uom_16: Optional[str] = None
    attribute_value_17: Optional[str] = None
    attribute_uom_17: Optional[str] = None
    attribute_value_18: Optional[str] = None
    attribute_uom_18: Optional[str] = None
    attribute_value_19: Optional[str] = None
    attribute_uom_19: Optional[str] = None
    attribute_value_20: Optional[str] = None
    attribute_uom_20: Optional[str] = None
    attribute_value_21: Optional[str] = None
    attribute_uom_21: Optional[str] = None
    attribute_value_22: Optional[str] = None
    attribute_uom_22: Optional[str] = None
    attribute_value_23: Optional[str] = None
    attribute_uom_23: Optional[str] = None
    attribute_value_24: Optional[str] = None
    attribute_uom_24: Optional[str] = None
    attribute_value_25: Optional[str] = None
    attribute_uom_25: Optional[str] = None
    attribute_value_26: Optional[str] = None
    attribute_uom_26: Optional[str] = None
    attribute_value_27: Optional[str] = None
    attribute_uom_27: Optional[str] = None
    attribute_value_28: Optional[str] = None
    attribute_uom_28: Optional[str] = None
    attribute_value_29: Optional[str] = None
    attribute_uom_29: Optional[str] = None
    attribute_value_30: Optional[str] = None
    attribute_uom_30: Optional[str] = None
    attribute_value_31: Optional[str] = None
    attribute_uom_31: Optional[str] = None
    attribute_value_32: Optional[str] = None
    attribute_uom_32: Optional[str] = None
    attribute_value_33: Optional[str] = None
    attribute_uom_33: Optional[str] = None
    attribute_value_34: Optional[str] = None
    attribute_uom_34: Optional[str] = None
    attribute_value_35: Optional[str] = None
    attribute_uom_35: Optional[str] = None
    attribute_value_36: Optional[str] = None
    attribute_uom_36: Optional[str] = None
    attribute_value_37: Optional[str] = None
    attribute_uom_37: Optional[str] = None
    attribute_value_38: Optional[str] = None
    attribute_uom_38: Optional[str] = None
    attribute_value_39: Optional[str] = None
    attribute_uom_39: Optional[str] = None
    attribute_value_40: Optional[str] = None
    attribute_uom_40: Optional[str] = None
    attribute_value_41: Optional[str] = None
    attribute_uom_41: Optional[str] = None
    attribute_value_42: Optional[str] = None
    attribute_uom_42: Optional[str] = None
    attribute_value_43: Optional[str] = None
    attribute_uom_43: Optional[str] = None
    attribute_value_44: Optional[str] = None
    attribute_uom_44: Optional[str] = None
    attribute_value_45: Optional[str] = None
    attribute_uom_45: Optional[str] = None
    attribute_value_46: Optional[str] = None
    attribute_uom_46: Optional[str] = None
    attribute_value_47: Optional[str] = None
    attribute_uom_47: Optional[str] = None
    attribute_value_48: Optional[str] = None
    attribute_uom_48: Optional[str] = None
    attribute_value_49: Optional[str] = None
    attribute_uom_49: Optional[str] = None
    attribute_value_50: Optional[str] = None
    attribute_uom_50: Optional[str] = None

    values: List[AttributeValue] = Relationship(back_populates="attribute")


    products: List["Product"] = Relationship(
    back_populates="attribute_objs",
    link_model=ProductAttributeLinkModel
)

class CategoryAttribute(UUIDModel, table=True):
    __tablename__ = "category_attributes"
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    category_id: UUID = Field(foreign_key="categories.id", index=True)
    attribute_id: UUID = Field(foreign_key="attribute_master.id", index=True)
    
    is_primary: bool = Field(default=False)
    display_order: int = Field(default=0)