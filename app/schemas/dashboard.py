from pydantic import BaseModel

class DashboardMetricsResponse(BaseModel):
    totalProjects: int
    activeProjects: int
    totalProducts: int
    publishedProducts: int
    catalogHealth: int