import logging
import os
import re
import json
import base64
import asyncio
from collections import deque
from time import monotonic
import uuid
from pathlib import Path
from typing import Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageEnhance, ImageOps
from io import BytesIO

from openai import OpenAI
from config import GROQ_API_KEY, RESERVE_GROQ_API_KEY, GROQ_OPENAI_BASE_URL as CONFIG_GROQ_OPENAI_BASE_URL

logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=2)

GROQ_REQUEST_INTERVAL_SECONDS = 5
GROQ_REQUESTS_PER_MINUTE = 5
GROQ_RATE_WINDOW_SECONDS = 60


class GroqRateLimiter:
    def __init__(self, interval_seconds: float, requests_per_window: int, window_seconds: float):
        self.interval_seconds = interval_seconds
        self.requests_per_window = requests_per_window
        self.window_seconds = window_seconds
        self._lock = asyncio.Lock()
        self._request_times = deque()
        self._next_allowed_at = 0.0

    async def wait_for_slot(self) -> None:
        async with self._lock:
            while True:
                now = monotonic()
                while self._request_times and now - self._request_times[0] >= self.window_seconds:
                    self._request_times.popleft()

                interval_wait = max(0.0, self._next_allowed_at - now)
                window_wait = 0.0
                if len(self._request_times) >= self.requests_per_window:
                    window_wait = max(0.0, self.window_seconds - (now - self._request_times[0]))

                wait_seconds = max(interval_wait, window_wait)
                if wait_seconds == 0:
                    request_started_at = monotonic()
                    self._request_times.append(request_started_at)
                    self._next_allowed_at = request_started_at + self.interval_seconds
                    return

                logger.info("Groq rate limit reached, waiting %.1f seconds", wait_seconds)
                await asyncio.sleep(wait_seconds)


groq_rate_limiter = GroqRateLimiter(
    interval_seconds=GROQ_REQUEST_INTERVAL_SECONDS,
    requests_per_window=GROQ_REQUESTS_PER_MINUTE,
    window_seconds=GROQ_RATE_WINDOW_SECONDS,
)

GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_OPENAI_BASE_URL = CONFIG_GROQ_OPENAI_BASE_URL


def get_image_orientation(image_bytes: bytes) -> Optional[int]:
    """Return EXIF orientation value or None if not available."""
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            exif = img._getexif()
            if not exif:
                return None
            return exif.get(274)
    except Exception:
        return None


def image_has_normal_orientation(image_bytes: bytes) -> bool:
    """Return True when the image orientation is already normal (no rotation needed)."""
    orientation = get_image_orientation(image_bytes)
    return orientation in (None, 1)


def preprocess_image(image_bytes: bytes) -> bytes:
    """Preprocess image by cropping center, enhancing shadows intelligently, and re-encoding as JPEG."""
    try:
        img = Image.open(BytesIO(image_bytes))
        img.load()

        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img = img.copy()

        width, height = img.size
        left = int(width * 0.25)
        top = int(height * 0.32)
        right = int(width * 0.75)
        bottom = int(height * 0.82)
        img = img.crop((left, top, right, bottom))
        
 
        gray_img = img.convert("L")
        

        rescaled_img = gray_img.point(lambda x: int(pow(x / 255.0, 0.7) * 255.0))
        

        enhancer = ImageEnhance.Contrast(rescaled_img)
        final_img = enhancer.enhance(1.5) 
        

        final_img = final_img.resize((800, 800), Image.Resampling.LANCZOS)
        
        final_rgb = final_img.convert("RGB")
        final_rgb.info.pop("exif", None)

        buffered = BytesIO()
        final_rgb.save(buffered, format="JPEG", quality=95)
        return buffered.getvalue()
    except Exception as e:
        logger.exception("Image preprocessing failed, using original bytes")
        return image_bytes


def _call_groq_sync(image_bytes: bytes) -> Optional[Dict[str, Any]]:
    """
    Synchronous call to the Groq OpenAI-compatible endpoint.
    Sends IMAGE_BASE64 with a prompt and parses the response text as JSON.
    """
    api_keys = [key for key in [GROQ_API_KEY, RESERVE_GROQ_API_KEY] if key]
    if not api_keys:
        logger.error("GROQ_API_KEY is not set in config or environment")
        return None

    if len(api_keys) == 2 and api_keys[0] == api_keys[1]:
        api_keys = [api_keys[0]]

    preprocessed = preprocess_image(image_bytes)
    b64 = base64.b64encode(preprocessed).decode("utf-8")

    prompt = (
        "Return strict JSON with keys: {\"systolic\": int, \"diastolic\": int, \"pulse\": int, \"date\": string|null, \"time\": string|null, \"comment\": string|null}. "
        "Do not include any extra text or markdown. JSON only."
    )

    for idx, api_key in enumerate(api_keys, start=1):
        client = OpenAI(api_key=api_key, base_url=GROQ_OPENAI_BASE_URL)
        logger.info(f"Calling Groq OpenAI-compatible endpoint (model={GROQ_MODEL}) using key #{idx}")

        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
                            },
                            {
                                "type": "text",
                                "text": prompt
                            }
                        ]
                    }
                ],
                temperature=0.0,
                max_tokens=512,
            )
            logger.info("Groq OpenAI-compatible chat.completions returned")

            message_content = resp.choices[0].message.content

            if "```" in message_content:

                import re
                json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', message_content, re.DOTALL)
                if json_match:
                    message_content = json_match.group(1).strip()

            try:
                parsed = json.loads(message_content)
                result = {k: v for k, v in parsed.items() if v is not None}
                if result:
                    return result
                logger.warning("Groq returned empty JSON result")
            except Exception as e:
                logger.warning(f"Failed to parse response as JSON: {e}")
        except Exception:
            logger.exception("Exception while calling Groq OpenAI-compatible API")

        if idx < len(api_keys):
            logger.warning("Primary Groq key failed, retrying with reserve key")

    return None


async def ocr_image_with_groq(image_bytes: bytes) -> Optional[Dict[str, Any]]:
    await groq_rate_limiter.wait_for_slot()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _call_groq_sync, image_bytes)
