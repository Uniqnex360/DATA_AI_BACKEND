
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.database import async_session_factory
from app.models.industry import Industry
from app.models.category import Category
from app.models.attribute import Attribute, CategoryAttribute
from app.models.attribute import Attribute, CategoryAttribute, AttributeValue  
from app.models.product import Product  
from app.models.brand import Brand  
from app.models.vendor import Vendor 
from uuid import uuid4

async def seed_categories():
    async with async_session_factory() as db:
        
        print(" Starting taxonomy seed...")
        
        
        
        
        lighting = Industry(name="Lighting", code="LIGHT")
        tools = Industry(name="Tools and Test Equipment", code="TOOLS")
        safety = Industry(name="Safety", code="SAFETY")
        
        db.add_all([lighting, tools, safety])
        await db.commit()
        await db.refresh(lighting)
        await db.refresh(tools)
        await db.refresh(safety)
        
        print(f" Created industries: {lighting.name}, {tools.name}, {safety.name}")
        
        
        
        
        
        
        lighting_cat = Category(
            name="High Bay Lighting",
            industry_id=lighting.id,
            full_path="Lighting > Indoor Lighting Fixtures > High Bay Lighting",
            level=3,
            is_leaf=True
        )
        db.add(lighting_cat)
        
        
        saw_blades_cat = Category(
            name="Saw Blades",
            industry_id=tools.id,
            full_path="Tools and Test Equipment > Cutting Tools and Metalworking > Saw Blades",
            level=3,
            is_leaf=True
        )
        db.add(saw_blades_cat)
        
        
        harnesses_cat = Category(
            name="Harnesses",
            industry_id=safety.id,
            full_path="Safety > Fall Protection > Harnesses",
            level=3,
            is_leaf=True
        )
        db.add(harnesses_cat)
        
        await db.commit()
        await db.refresh(lighting_cat)
        await db.refresh(saw_blades_cat)
        await db.refresh(harnesses_cat)
        
        print(f"Created categories:")
        print(f"   - {lighting_cat.full_path}")
        print(f"   - {saw_blades_cat.full_path}")
        print(f"   - {harnesses_cat.full_path}")
        
        
        
        
        
        
        color_temp = Attribute(
            name="color_temperature",
            display_name="Color Temperature",
            data_type="number",
            default_uom="K"
        )
        lumens = Attribute(
            name="lumens",
            display_name="Lumens",
            data_type="number",
            default_uom="lm"
        )
        input_watts = Attribute(
            name="input_watts",
            display_name="Input Watts",
            data_type="number",
            default_uom="W"
        )
        distribution = Attribute(
            name="distribution",
            display_name="Distribution",
            data_type="string"
        )
        light_tech = Attribute(
            name="light_technology",
            display_name="Light Technology",
            data_type="string"
        )
        
        db.add_all([color_temp, lumens, input_watts, distribution, light_tech])
        
        
        blade_material = Attribute(
            name="blade_material",
            display_name="Blade Material",
            data_type="string"
        )
        diameter = Attribute(
            name="diameter",
            display_name="Diameter",
            data_type="number",
            default_uom="inches"
        )
        length = Attribute(
            name="length",
            display_name="Length",
            data_type="number",
            default_uom="inches"
        )
        teeth_count = Attribute(
            name="teeth_count",
            display_name="Teeth Count",
            data_type="number"
        )
        application = Attribute(
            name="application",
            display_name="Application",
            data_type="string"
        )
        
        db.add_all([blade_material, diameter, length, teeth_count, application])
        
        
        buckle_type = Attribute(
            name="buckle_type_chest",
            display_name="Buckle Type - Chest",
            data_type="string"
        )
        d_rings = Attribute(
            name="number_of_d_rings",
            display_name="Number of D-Rings",
            data_type="number"
        )
        d_ring_location = Attribute(
            name="d_ring_location",
            display_name="D-Ring Location",
            data_type="string"
        )
        harness_style = Attribute(
            name="harness_style",
            display_name="Harness Style",
            data_type="string"
        )
        webbing_material = Attribute(
            name="webbing_material",
            display_name="Webbing Material",
            data_type="string"
        )
        
        db.add_all([buckle_type, d_rings, d_ring_location, harness_style, webbing_material])
        
        await db.commit()
        print(f"Created 15 attributes")
        
        
        
        
        
        
        await db.refresh(color_temp)
        await db.refresh(lumens)
        await db.refresh(input_watts)
        await db.refresh(distribution)
        await db.refresh(light_tech)
        
        for i, attr in enumerate([color_temp, lumens, input_watts, distribution, light_tech], 1):
            cat_attr = CategoryAttribute(
                category_id=lighting_cat.id,
                attribute_id=attr.id,
                is_primary=True,
                display_order=i,
                required=True if i <= 3 else False
            )
            db.add(cat_attr)
        
        
        await db.refresh(blade_material)
        await db.refresh(diameter)
        await db.refresh(length)
        await db.refresh(teeth_count)
        await db.refresh(application)
        
        for i, attr in enumerate([blade_material, diameter, length, teeth_count, application], 1):
            cat_attr = CategoryAttribute(
                category_id=saw_blades_cat.id,
                attribute_id=attr.id,
                is_primary=True,
                display_order=i
            )
            db.add(cat_attr)
        
        
        await db.refresh(buckle_type)
        await db.refresh(d_rings)
        await db.refresh(d_ring_location)
        await db.refresh(harness_style)
        await db.refresh(webbing_material)
        
        for i, attr in enumerate([buckle_type, d_rings, d_ring_location, harness_style, webbing_material], 1):
            cat_attr = CategoryAttribute(
                category_id=harnesses_cat.id,
                attribute_id=attr.id,
                is_primary=True,
                display_order=i
            )
            db.add(cat_attr)
        
        await db.commit()
        print(f" Linked attributes to categories")
        
        print("\n Seed complete!")
        print(" 3 industries")
        print(" 3 categories")
        print("15 attributes")
        print(" 15 category-attribute links")
        print("\n Database is ready for aggregation with taxonomy hints!")

if __name__ == "__main__":
    asyncio.run(seed_categories())