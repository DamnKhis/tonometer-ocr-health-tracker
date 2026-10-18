import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials
from config import GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_FILE
import logging
from typing import Dict, Any
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=1)
_gspread_client = None

def get_google_sheet_client():
    global _gspread_client
    if _gspread_client is not None:
        return _gspread_client

    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        credentials = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=scope
        )
        _gspread_client = gspread.authorize(credentials)
        return _gspread_client
    except Exception as e:
        logger.error(f"Error creating Google Sheets client: {e}")
        return None

def _get_user_worksheet(spreadsheet, user_id: int, create_if_missing: bool = False):
    sheet_name = str(user_id)
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
        logger.info(f"Found existing worksheet for user {sheet_name}")
        return worksheet
    except WorksheetNotFound:
        if not create_if_missing:
            return None

        logger.info(f"Creating new worksheet for user {sheet_name}")
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=6)
        headers = ["Дата", "Время", "Систолическое", "Диастолическое", "Пульс", "Примечание"]
        worksheet.append_row(headers)
        try:
            worksheet.format("A1:F1", {"textFormat": {"bold": True}})
            worksheet.freeze(rows=1)
        except Exception:
            logger.warning("Could not apply header formatting, continuing without it.")
        return worksheet

def _ordered_data(data: Dict[str, Any]) -> list[str]:
    return [
        "" if data.get("date") is None else str(data.get("date")),
        "" if data.get("time") is None else str(data.get("time")),
        "" if data.get("systolic") is None else str(data.get("systolic")),
        "" if data.get("diastolic") is None else str(data.get("diastolic")),
        "" if data.get("pulse") is None else str(data.get("pulse")),
        "" if data.get("comment") is None else str(data.get("comment")),
    ]

def _row_exists_in_sheet_sync(data: Dict[str, Any], user_id: int) -> bool:
    client = get_google_sheet_client()
    if not client:
        return False

    try:
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        worksheet = _get_user_worksheet(spreadsheet, user_id)
        if worksheet is None:
            return False

        expected_row = _ordered_data(data)
        return any(
            [str(cell) for cell in row[:6]] + [""] * (6 - len(row[:6])) == expected_row
            for row in worksheet.get_all_values()[1:]
        )
    except Exception as e:
        logger.error(f"Error checking Google Sheet for existing data: {e}", exc_info=True)
        return False

def _append_row_to_sheet_sync(data: Dict[str, Any], user_id: int) -> bool:
    """
    Синхронно добавляет строку данных в Google Sheet на лист пользователя.
    """
    logger.info("Getting Google Sheets client...")
    client = get_google_sheet_client()
    if not client:
        logger.error("Failed to create Google Sheets client")
        return False

    try:
        logger.info(f"Opening spreadsheet with ID: {GOOGLE_SHEET_ID}")
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        logger.info("Spreadsheet opened successfully")

        sheet_name = str(user_id)
        worksheet = _get_user_worksheet(spreadsheet, user_id, create_if_missing=True)
        ordered_data = _ordered_data(data)
        logger.info(f"Appending data for user {sheet_name}: {ordered_data}")
        worksheet.append_row(ordered_data)
        try:
            worksheet.sort((1, "asc"), (2, "asc"))
            logger.info(f"Sorted worksheet {sheet_name} by date/time ascending")
        except Exception as e:
            logger.warning(f"Could not sort worksheet {sheet_name}: {e}")
        logger.info(f"Data successfully appended to Google Sheet for user {sheet_name}")
        return True
    except Exception as e:
        logger.error(f"Error appending data to Google Sheet: {e}", exc_info=True)
        return False

async def append_row_to_sheet(data: Dict[str, Any], user_id: int) -> bool:
    """
    Асинхронно добавляет строку данных в Google Sheet на лист пользователя.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _append_row_to_sheet_sync, data, user_id)

async def row_exists_in_sheet(data: Dict[str, Any], user_id: int) -> bool:
    """Асинхронно проверяет, есть ли такая строка на листе пользователя."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _row_exists_in_sheet_sync, data, user_id)
