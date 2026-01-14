from typing import Any, Dict, Generic, List, Optional, Type, TypeVar, Union
from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.encoders import jsonable_encoder
ModelType=TypeVar("ModelType",bound=SQLModel)
CreateSchemaType=TypeVar("CreateSchemaType",bound=SQLModel)
UpdateSchemaType=TypeVar('UpdateSchemaType',bound=SQLModel)

class CRUDBase(Generic[ModelType,CreateSchemaType,UpdateSchemaType]):
    def __init__(self,model:Type[ModelType]):
        self.model=model
    async def get(self,db:AsyncSession,id:Any)->Optional[ModelType]:
        return await db.get(self.model,id)
    async def get_multi(self,db:AsyncSession,*,skip:int=0,limit:int=100)->List[ModelType]:
        query=select(self.model).offset(skip).limit(limit)
        result=await db.exec(query)
        return result.all()
    async def create(self,db:AsyncSession,*,obj_in:CreateSchemaType)->ModelType:
        obj_in_data=jsonable_encoder(obj_in)
        db_obj=self.model(**obj_in_data)
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    async def update(self,db:AsyncSession,*,db_obj:ModelType,obj_in:Union[UpdateSchemaType,Dict[str,Any]])->ModelType:
        obj_data=jsonable_encoder(db_obj)
        if isinstance(obj_data,dict):
            update_data=obj_in
        else:
            update_data=obj_in.dict(exclude_unset=True)
        for field in obj_data:
            if field in update_data:
                setattr(db_obj,field,update_data[field])
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    async def remove(self,db:AsyncSession,*,id:Any)->ModelType:
        obj=await db.get(self.model,id)
        await db.delete()
        await db.commit()
        return obj
        
        