from  typing import List,Optional,Dict
from pydantic import BaseModel,Field



class FreshAggregationRequest(BaseModel):
    mpns:List[str]
    project_id:str
    use_case:str
    detailed_data: Optional[List[Dict]] = None 

class PDFExtractionResponse(BaseModel):
    product_name: Optional[str] = ""
    brand_name: Optional[str] = ""
    sku: Optional[str] = ""
    taxonomy: Optional[str] = ""
    description: Optional[str] = ""
    image_url: Optional[str] = ""
    specifications: Optional[Dict[str, str]] = {}

class StructuredExtractionRequest(BaseModel):
    mpn:str
    project_id:str
    use_case:str
class SingleProductExtraction(BaseModel):
    data: Dict[str, PDFExtractionResponse] = Field(default_factory=dict)
class ProductIdentificationItem(BaseModel):
    title: str
    context: str
    confidence: float

class ProductIdentificationResponse(BaseModel):
    products: List[ProductIdentificationItem]