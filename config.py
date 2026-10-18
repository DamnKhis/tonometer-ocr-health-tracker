import os
from pathlib import Path
from dotenv import load_dotenv

dotenv_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")


_service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
if not os.path.isabs(_service_account_file):
    _service_account_file = str(Path(__file__).parent / "utils" / _service_account_file)
GOOGLE_SERVICE_ACCOUNT_FILE = _service_account_file


GROQ_API_KEY = os.getenv("GROQ_API_KEY")
RESERVE_GROQ_API_KEY = os.getenv("RESERVE_GROQ_API_KEY")
DOG_STICKER_ID = os.getenv("DOG_STICKER_ID")
GROQ_OPENAI_BASE_URL = os.getenv("GROQ_OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
