from pathlib import Path
import logging
from typing import Optional, Dict
import cloudinary
import cloudinary.uploader
from .config import settings
logger = logging.getLogger(__name__)
cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
    secure=True
)


# def upload_source(file_content: bytes, public_id: str) -> Optional[Dict]:
#     if not file_content:
#         logger.warning("Empty file content, skipping upload")
#         return None
#     if not public_id:
#         logger.error("Missing public_id for Cloudinary upload")
#         return None
#     try:
#         result = cloudinary.uploader.upload(
#             file_content,
#             resource_type="raw",
#             public_id=public_id,  
#             overwrite=True,
#             tags=["source", "permanent"]
#         )
#         return {
#             "public_id": result.get("public_id"),
#             "secure_url": result.get("secure_url"),
#             "bytes": result.get("bytes"),
#             "created_at": result.get("created_at")
#         }
#     except Exception as e:
#         logger.error(f"Cloudinary upload failed ({public_id}): {e}")
#         return None
def upload_source(file_content: bytes, public_id: str):
    try:
        # 1. Create a storage folder if it doesn't exist
        storage_path = Path("./storage")
        storage_path.mkdir(exist_ok=True)
        
        # 2. Define the local file path
        # Use .pdf or .html based on content if you want, or just no extension
        file_path = storage_path / f"{public_id}"
        
        # 3. Write the file to your hard drive
        file_path.write_bytes(file_content)
        
        # 4. Return the local path as the "url"
        return {
            "secure_url": str(file_path.absolute()),
            "public_id": public_id
        }
    except Exception as e:
        print(f"Local save failed: {e}")
        return None