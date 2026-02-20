import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func,case
from app.core.database import get_session
from app.models.product import Product
from app.models.project import Project
from app.schemas.dashboard import DashboardMetricsResponse
logger = logging.getLogger("dashboard_metrics")
router = APIRouter()

async def calculate_metrics(db:AsyncSession,project_id:str|None)->dict:
    try:
        product_filters=[]
        if project_id:
            product_filters.append(Product.project_id==project_id)
        stmt = select(
        func.count(Product.id).label("total"),
        func.sum(case((Product.enrichment_status == 'completed', 1), else_=0)).label("aggregated"),
        func.sum(case((Product.enrichment_status == 'failed', 1), else_=0)).label("failed"),
        func.sum(case((Product.enrichment_status == 'pending', 1), else_=0)).label("pending"),
        func.avg(Product.completeness_score).label("health")
    )
        if project_id:
            stmt=stmt.where(Product.project_id==project_id)
        result=await db.execute(stmt)
        stats=result.first()
        cat_expression = func.json_extract_path_text(Product.attributes, 'taxonomy')
        cat_stmt = (select(cat_expression, func.count(Product.id)).where(*product_filters).group_by(cat_expression).order_by(func.count(Product.id).desc()).limit(5))
        cat_result=await db.execute(cat_stmt)
        categories = [{"category_name": row[0] or "Uncategorized", "count": row[1]} for row in cat_result.all()]
        total_projects=0
        active_projects=0
        project_name='Global Overview'
        if project_id:
            proj=await db.get(Project,project_id)
            if proj:
                project_name=proj.name 
                total_projects=1
                active_projects=1 if proj.status=='active' else 0
        else:
            proj_stats = await db.execute(
            select(func.count(Project.id),func.sum(case((Project.status == 'active', 1), else_=0))))
            p_row=proj_stats.first()
            total_projects=p_row[0] or 0
            active_projects = p_row[1] or 0
            
        completed_count = stats.aggregated or 0
        return {
        "name": project_name,
        "totalProjects": total_projects,
        "activeProjects": active_projects,
        "totalProducts": stats.total or 0,
        
        "aggregatedProducts": completed_count,
        "cleanedProducts": completed_count,      
        "standardizedProducts": completed_count, 
        "enrichedProducts": completed_count,
        
        "publishedProducts": completed_count, 
        "failedProducts": stats.failed or 0,
        "pendingProducts": stats.pending or 0,
        "catalogHealth": int(stats.health or 0),
        "categoryDistribution": categories
    }
        
    except Exception as e:
        raise e
@router.get("/metrics", response_model=DashboardMetricsResponse)
async def get_dashboard_metrics(db: AsyncSession = Depends(get_session)):
    try:
        # total_projects_stmt = select(func.count(Project.id))
        # active_projects_stmt = select(func.count(Project.id)).where(Project.status == "active")
        # total_projects_res = await db.execute(total_projects_stmt)
        # active_projects_res = await db.execute(active_projects_stmt)
        # total_projects = total_projects_res.scalar() or 0
        # active_projects = active_projects_res.scalar() or 0
        # total_products_stmt = select(func.count(Product.id))
        # published_products_stmt = select(func.count(Product.id)).where(Product.published_at.is_not(None))
        # total_products_res = await db.execute(total_products_stmt)
        # published_products_res = await db.execute(published_products_stmt)
        # total_products = total_products_res.scalar() or 0
        # published_products = published_products_res.scalar() or 0
        # health_stmt = select(func.avg(Product.completeness_score))
        # health_res = await db.execute(health_stmt)
        # avg_health = health_res.scalar() or 0
        # logger.info(f"Dashboard refreshed: {total_products} products, {total_projects} projects")
        # return {
        #     "totalProjects": total_projects,
        #     "activeProjects": active_projects,
        #     "totalProducts": total_products,
        #     "publishedProducts": published_products,
        #     "catalogHealth": int(avg_health) 
        # }
        data=await calculate_metrics(db,None)
        logger.info(f"Global dashboard metrics loaded :{data['totalProducts']} products")
        return data
    except Exception as e:
        logger.error(f"CRITICAL: Failed to calculate dashboard metrics: {str(e)}")
        return {
            "totalProjects": 0, "activeProjects": 0, "totalProducts": 0,
            "publishedProducts": 0, "catalogHealth": 0, "categoryDistribution": []
        }
        
@router.get('/metrics/{project_id}',response_model=DashboardMetricsResponse)
async def get_project_metrics(project_id:str,db:AsyncSession=Depends(get_session)):
    try:
        # total_projects_stmt=select(func.count(Project.id))
        # active_projects_stmt=select(func.count(Project.id)).where(Project.status=='active')
        # total_projects_res=await db.execute(total_projects_stmt)
        # active_projects_res=await db.execute(active_projects_stmt)
        # total_projects=total_projects_res.scalar() or 0 
        # active_projects=active_projects_res.scalar() or 0 
        
        # prod_stmt=select(func.count(Product.id)).where(Product.project_id==project_id)
        # prod_res=await db.execute(prod_stmt)
        # total_products=prod_res.scalar() or 0 
        # pub_stmt=select(func.count(Product.id)).where(Product.project_id==project_id, Product.published_at.is_not(None))    
        # pub_res=await db.execute(pub_stmt)
        # published=pub_res.scalar() or 0 
        # health_stmt=select(func.avg(Product.completeness_score)).where(Product.project_id==project_id)
        # health_res=await db.execute(health_stmt)
        # avg_health=health_res.scalar() or 0 
        # return {
        #     "totalProjects":total_projects,
        #     'activeProjects':active_projects,
        #     'totalProducts':total_products,
        #     'publishedProducts':published,
        #     'catalogHealth':int(avg_health)
        # }
        data = await calculate_metrics(db, project_id)
        logger.info(f"Project metrics loaded for {project_id}")
        return data
    except Exception as e:
        logger.error(f"Failed to fetch project metrics for  {project_id}:{str(e)}")
        return {
            "totalProjects": 0, "activeProjects": 0, "totalProducts": 0,
            "publishedProducts": 0, "catalogHealth": 0, "categoryDistribution": []
        }
