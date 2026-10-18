from datetime import datetime
from PIL import Image
from PIL.ExifTags import TAGS
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

def get_exif_data(image_path: str) -> Optional[Dict[str, Any]]:

    try:
        with Image.open(image_path) as img:
            exif_data = img._getexif()
            if exif_data is None:
                logger.info(f"No EXIF data found in {image_path}")
                return None

            decoded_exif = {}
            for tag, value in exif_data.items():
                decoded = TAGS.get(tag, tag)
                decoded_exif[decoded] = value
            return decoded_exif
    except Exception as e:
        logger.error(f"Error extracting EXIF data from {image_path}: {e}")
        return None

def extract_date_time_from_exif(exif_data: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    date_time_original = exif_data.get("DateTimeOriginal") or exif_data.get("DateTime")
    if date_time_original:
        try:
            dt_object = datetime.strptime(date_time_original, "%Y:%m:%d %H:%M:%S")
            return dt_object.strftime("%Y-%m-%d"), dt_object.strftime("%H:%M")
        except ValueError as e:
            logger.warning(f"Could not parse DateTimeOriginal \'{date_time_original}\': {e}")
    return None, None
