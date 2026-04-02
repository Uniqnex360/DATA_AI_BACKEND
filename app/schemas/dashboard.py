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

class CategoryStat(BaseModel):
    category_name: str
    count: int

class TimelineStat(BaseModel):
    period: str
    totalProducts: int = 0
    aggregatedProducts: int = 0
    movedToEnrichment: int = 0

class BrandFlowStat(BaseModel):
    brand: str
    totalProducts: int = 0
    aggregatedProducts: int = 0
    enrichmentProducts: int = 0

class BrandAttributeStat(BaseModel):
    brand: str
    aggregationAttributes: int = 0
    enrichmentAttributes: int = 0
    completedAttributes: int = 0
    totalAttributes: int = 0

class CategoryDistributionStat(BaseModel):
    category: str
    productCount: int
    percentage: float

class CategoryFlowStat(BaseModel):
    category: str
    totalProducts: int
    aggregatedProducts: int
    enrichmentProducts: int

class CategoryAttributeStat(BaseModel):
    category: str
    totalAttributes: int
    aggregationAttributes: int
    enrichmentAttributes: int
    completedAttributes: int