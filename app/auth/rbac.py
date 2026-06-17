from typing import List, Optional

from sqlalchemy import BinaryExpression
import logging
from app.models.user import User
from app.models.project import Project
logger=logging.getLogger(__name__)


def get_auth_filters(current_user:User,target_user_id:Optional[str]=None)->List[BinaryExpression]:
    try:
        if current_user.role=='admin':
            if target_user_id and target_user_id!='all':
                return [Project.owner_id==target_user_id]
            return []
        return [Project.owner_id==current_user.id]
    except Exception as e:
        logger.exception("Error generating authorization filters")
        raise RuntimeError(f"Failed to generate authorization filters: {str(e)}") 
