#!/usr/bin/env python3
"""
Debug script to diagnose why products show 0 count in AWS deployment
Run this to check if products are actually in the database and their project_id values
"""

import asyncio
import sys
from app.core.database import async_session_factory
from app.models.product import Product
from app.models.project import Project
from sqlmodel import select, func
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def debug_check():
    """Debug the product count issue"""
    async with async_session_factory() as session:
        try:
            # 1. Check total products in database
            total_products_stmt = select(func.count(Product.id))
            total_result = await session.execute(total_products_stmt)
            total_products = total_result.scalar() or 0
            print(f"\n✓ Total products in database: {total_products}")
            
            # 2. Check all projects
            projects_stmt = select(Project)
            projects_result = await session.execute(projects_stmt)
            projects = projects_result.scalars().all()
            print(f"\n✓ Total projects in database: {len(projects)}")
            
            if projects:
                print("\nProject Details:")
                print("-" * 80)
                for project in projects:
                    print(f"  Project ID: {project.id}")
                    print(f"  Project ID Type: {type(project.id)}")
                    print(f"  Project ID as string: '{str(project.id)}'")
                    print(f"  Project Name: {project.name}")
                    
                    # 3. For each project, count its products
                    project_id_str = str(project.id)
                    products_stmt = select(func.count(Product.id)).where(
                        Product.project_id == project_id_str
                    )
                    products_result = await session.execute(products_stmt)
                    product_count = products_result.scalar() or 0
                    print(f"  Products with project_id = '{project_id_str}': {product_count}")
                    
                    # 4. Show actual products for this project
                    actual_products_stmt = select(Product).where(
                        Product.project_id == project_id_str
                    )
                    actual_result = await session.execute(actual_products_stmt)
                    actual_products = actual_result.scalars().all()
                    if actual_products:
                        print(f"  Products:")
                        for prod in actual_products[:5]:  # Show first 5
                            print(f"    - {prod.product_code} (project_id: '{prod.project_id}')")
                        if len(actual_products) > 5:
                            print(f"    ... and {len(actual_products) - 5} more")
                    print()
            
            # 5. Check all unique project_ids in Product table
            project_ids_stmt = select(Product.project_id).distinct()
            project_ids_result = await session.execute(project_ids_stmt)
            project_ids = project_ids_result.scalars().all()
            print("\nUnique project_ids in Product table:")
            print("-" * 80)
            for pid in project_ids:
                count_stmt = select(func.count(Product.id)).where(Product.project_id == pid)
                count_result = await session.execute(count_stmt)
                count = count_result.scalar() or 0
                print(f"  project_id: '{pid}' (type: {type(pid).__name__}) -> {count} products")
            
            # 6. Show some sample products and their project_ids
            sample_stmt = select(Product).limit(10)
            sample_result = await session.execute(sample_stmt)
            samples = sample_result.scalars().all()
            if samples:
                print("\nSample products (first 10):")
                print("-" * 80)
                for prod in samples:
                    print(f"  SKU: {prod.product_code}")
                    print(f"    project_id: '{prod.project_id}' (type: {type(prod.project_id).__name__})")
                    print()
                    
        except Exception as e:
            logger.error(f"Error during debug check: {e}", exc_info=True)
            return False
    
    return True

if __name__ == "__main__":
    success = asyncio.run(debug_check())
    sys.exit(0 if success else 1)
