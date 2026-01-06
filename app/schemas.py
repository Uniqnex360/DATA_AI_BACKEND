from pydantic import BaseModel
from typing import List,Dict,Optional,Union
class RawValue(BaseModel):
    value:str
    source:str
class AggregatedAttribute(BaseModel):
    values:List[RawValue]
class CleaningResult(BaseModel):
    valid_values:List[RawValue]
    removed_values:List[Dict[str,str]]
class StandardizedAttribute(BaseModel):
    standard_value:Union[str,int,float,bool]
    unit:Optional[str]=None
    derived_from:List[str]
    confidence:float
    reason:str
class EnrichmentResult(BaseModel):
    seo_title:Optional[str]=None
    bullets:List[str]
    tags:List[str]
    use_cases:List[str]
    confidence:float
class ReviewItem(BaseModel):
    product_key:str
    attribute:str
    proposed_value:Union[str,int,float,bool]
    confidence:float
    reason:str
    derived_from:List[str]
    status:str='pending'
    reviewer:Optional[str]=None
    overridden_value:Optional[Union[str,float,int,bool]]=None   
class AggregateRequest(BaseModel):
    mpn: str = None
    upc: str = None
    title: str = None