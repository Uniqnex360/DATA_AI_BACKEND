from  typing import List
from openai import BaseModel


class FreshAggregationRequest(BaseModel):
    mpns:List[str]
    project_id:str
    use_case:str