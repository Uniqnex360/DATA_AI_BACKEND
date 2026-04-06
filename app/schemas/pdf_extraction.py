from  typing import List,Optional,Dict
from pydantic import BaseModel


class FreshAggregationRequest(BaseModel):
    mpns:List[str]
    project_id:str
    use_case:str

class PDFExtractionResponse(BaseModel):
    product_name: Optional[str] = ""
    brand_name: Optional[str] = ""
    sku: Optional[str] = ""
    taxonomy: Optional[str] = ""
    description: Optional[str] = ""
    image_url: Optional[str] = ""
    specifications: Optional[Dict[str, str]] = {}