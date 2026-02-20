from pydantic import BaseModel
from typing import Optional,List
class CategoryStat(BaseModel):
    category_name:str
    count:int
    
class DashboardMetricsResponse(BaseModel):
    totalProjects: int
    activeProjects: int
    totalProducts: int
    publishedProducts: int
    catalogHealth: int
    name:Optional[str]="Overview"
    aggregatedProducts:Optional[int]=0
    cleanedProducts:Optional[int]=0
    standardizedProducts:Optional[int]=0
    enrichedProducts:Optional[int]=0
    failedProducts:Optional[int]=0
    pendingProducts:Optional[int]=0 
    categoryDistribution: Optional[List[CategoryStat]] = []