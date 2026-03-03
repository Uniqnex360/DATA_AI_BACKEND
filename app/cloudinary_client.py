import logging
import cloudinary
import cloudinary.uploader
from app.core.config import settings 
logger = logging.getLogger(__name__)
cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
    secure=True
)


def upload_source(file_content: bytes, public_id: str):
    if not file_content:
        return None
    try:
        result = cloudinary.uploader.upload(
            file_content,
            resource_type="raw", 
            public_id=public_id,  
            overwrite=True
        )
        return {
            "secure_url": result.get("secure_url"),
            "public_id": result.get("public_id")
        }
    except Exception as e:
        logger.error(f"Cloudinary failed: {e}")
        return None