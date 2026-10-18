import logging
import os
import re
import tempfile
import uuid
from typing import Dict, Any
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramRetryAfter
import asyncio

from utils.exif import get_exif_data, extract_date_time_from_exif
from utils.vision import image_has_normal_orientation, ocr_image_with_groq
from utils.sheets import append_row_to_sheet, row_exists_in_sheet

logger = logging.getLogger(__name__)

photo_router = Router()

class BloodPressureCallback(CallbackData, prefix="bp"):
    action: str
    data_id: str
    field: str = ""

class EditBPField(StatesGroup):
    waiting_for_new_value = State()

FIELD_TITLES = {
    "systolic": "Верхнее",
    "diastolic": "Нижнее",
    "pulse": "Пульс",
    "date": "Дата",
    "time": "Время",
    "comment": "Примечание"
}

FIELD_PROMPTS = {
    "systolic": "Введите новое значение для верхнего давления (целое число).",
    "diastolic": "Введите новое значение для нижнего давления (целое число).",
    "pulse": "Введите новое значение для пульса (целое число).",
    "date": "Введите дату в формате YYYY-MM-DD или оставьте пустым.",
    "time": "Введите время в формате HH:MM или оставьте пустым.",
    "comment": "Введите новое примечание или оставьте пустым."
}

def build_confirmation_text(data: Dict[str, Any]) -> str:
    systolic = data.get("systolic", "N/A")
    diastolic = data.get("diastolic", "N/A")
    pulse = data.get("pulse", "N/A")
    date = data.get("date", "N/A")
    time = data.get("time", "N/A")
    comment = data.get("comment")

    comment_block = f"Примечание: {comment}\n" if comment else ""
    return (
        f"Распознанные данные:\n"
        f"Верхнее: {systolic}\n"
        f"Нижнее: {diastolic}\n"
        f"Пульс: {pulse}\n"
        f"Дата: {date}\n"
        f"Время: {time}\n"
        f"{comment_block}\n"
        "Проверьте данные и нажмите Сохранить или отредактируйте поле."
    )

def build_confirmation_keyboard(data_id: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Сохранить", callback_data=BloodPressureCallback(action="save", data_id=data_id))
    builder.button(text="Изменить верхнее", callback_data=BloodPressureCallback(action="edit", data_id=data_id, field="systolic"))
    builder.button(text="Изменить нижнее", callback_data=BloodPressureCallback(action="edit", data_id=data_id, field="diastolic"))
    builder.button(text="Изменить пульс", callback_data=BloodPressureCallback(action="edit", data_id=data_id, field="pulse"))
    builder.adjust(2)
    builder.button(text="Изменить дату", callback_data=BloodPressureCallback(action="edit", data_id=data_id, field="date"))
    builder.button(text="Изменить время", callback_data=BloodPressureCallback(action="edit", data_id=data_id, field="time"))
    builder.button(text="Изменить примечание", callback_data=BloodPressureCallback(action="edit", data_id=data_id, field="comment"))
    builder.adjust(2)
    return builder


async def process_image_and_recognize(image_path: str) -> Dict[str, Any]:

    logger.info(f"Processing image from {image_path}")
    
    logger.info("Extracting EXIF data...")
    exif_data = get_exif_data(image_path)
    exif_date, exif_time = None, None
    if exif_data:
        exif_date, exif_time = extract_date_time_from_exif(exif_data)
        if exif_date and exif_time:
            logger.info(f"EXIF data found: Date={exif_date}, Time={exif_time}")

    logger.info("Reading image bytes...")
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    logger.info(f"Image loaded. Size: {len(image_bytes)} bytes")
    logger.info("Calling OCR with Groq...")
    ocr_data = await ocr_image_with_groq(image_bytes) 
    logger.info(f"OCR returned: {ocr_data}")

    if ocr_data:
        if exif_date and exif_time:
            ocr_data["date"] = ocr_data.get("date") or exif_date
            ocr_data["time"] = ocr_data.get("time") or exif_time
        logger.info(f"Final recognition data: {ocr_data}")
        return ocr_data
    
    logger.warning("OCR returned None, using EXIF data only")
    return {"systolic": None, "diastolic": None, "pulse": None, "date": exif_date, "time": exif_time, "comment": None}


@photo_router.message(F.photo | F.document)
async def handle_photo_document(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает полученные фото и документы.
    """
    logger.info(f"Received photo/document from user {message.from_user.id}")
    
    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        file_size = photo.file_size
    else:
        photo = message.document
        file_id = photo.file_id
        file_size = photo.file_size if hasattr(photo, 'file_size') else None

    if file_size and file_size > 20 * 1024 * 1024: # 20 MB limit
        await message.reply("Файл слишком большой. Пожалуйста, отправьте изображение размером до 20 МБ.")
        return

    downloaded_file_path = None
    try:
        logger.info(f"Downloading file {file_id}")
        file_info = await message.bot.get_file(file_id)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_file:
            downloaded_file_path = tmp_file.name

        await message.bot.download_file(file_info.file_path, downloaded_file_path)
        logger.info(f"File downloaded to {downloaded_file_path}")

        with open(downloaded_file_path, "rb") as f:
            image_bytes = f.read()

        if image_has_normal_orientation(image_bytes):
            logger.info("Image has normal orientation.")
        else:
            logger.info("Image orientation is not normal; processing without sticker.")

        await message.reply("Обрабатываю изображение...")
        logger.info("Processing image with Groq...")

        recognized_data = await process_image_and_recognize(downloaded_file_path)
        logger.info(f"Recognition result: {recognized_data}")

        if getattr(message, "caption", None):
            caption_text = message.caption.strip()
            if caption_text:
                existing_comment = recognized_data.get("comment")
                recognized_data["comment"] = (
                    f"{existing_comment} | {caption_text}" if existing_comment else caption_text
                )
                logger.info(f"Added message caption to comment: {recognized_data['comment']}")

        null_count = sum(1 for value in recognized_data.values() if value is None)
        if null_count >= 3: 
            try:
                await message.reply(
                    "Не удалось распознать все данные. Пожалуйста, попробуйте перефотографировать или "
                    "введите данные вручную. (Функция ручного ввода пока не реализована)"
                )
            except TelegramRetryAfter as e:
                logger.warning(f"Flood control, waiting {e.retry_after} seconds")
                await asyncio.sleep(e.retry_after + 1)
                await message.reply("Флуд контроль. Подождите и попробуйте позже.")
            return

        if await row_exists_in_sheet(recognized_data, message.from_user.id):
            await message.reply("Такая строка уже есть в Google Sheets. Данные не нужно сохранять повторно.")
            return

        data_id = uuid.uuid4().hex
        await state.update_data({data_id: recognized_data})
        logger.info(f"State updated with data_id: {data_id}")

        recognized_text = build_confirmation_text(recognized_data)
        builder = build_confirmation_keyboard(data_id)

        try:
            await message.reply(recognized_text, reply_markup=builder.as_markup())
            logger.info("Confirmation message sent successfully")
        except TelegramRetryAfter as e:
            logger.warning(f"Flood control on confirmation, waiting {e.retry_after} seconds")
            await asyncio.sleep(e.retry_after + 1)
            await message.reply(recognized_text, reply_markup=builder.as_markup())
    except Exception as e:
        logger.error(f"Error in handle_photo_document: {e}", exc_info=True)
        try:
            await message.reply("Произошла ошибка при обработке. Попробуйте позже.")
        except Exception:
            logger.error("Failed to send error message")
    finally:
        if downloaded_file_path:
            try:
                os.remove(downloaded_file_path)
            except OSError:
                pass

@photo_router.callback_query(BloodPressureCallback.filter(F.action == "save"))
async def handle_save_callback(call: CallbackQuery, callback_data: BloodPressureCallback, state: FSMContext) -> None:
    logger.info(f"Save button clicked with data_id: {callback_data.data_id}")
    
    user_data = await state.get_data()
    recognized_data = user_data.get(callback_data.data_id)
    logger.info(f"Retrieved data from state: {recognized_data}")

    if not recognized_data:
        logger.warning(f"Data not found for data_id: {callback_data.data_id}")
        await call.answer("Данные для сохранения не найдены.", show_alert=True)
        return

    await state.set_data({key: value for key, value in user_data.items() if key != callback_data.data_id})

    try:
        if await row_exists_in_sheet(recognized_data, call.from_user.id):
            await call.message.edit_text("Такая строка уже есть в Google Sheets. Данные не сохранены повторно.")
            await call.answer()
            return

        logger.info(f"Saving data to Google Sheets: {recognized_data}")
        if await append_row_to_sheet(recognized_data, call.from_user.id):
            logger.info("Data successfully saved to Google Sheets")
            await call.message.edit_text("Данные успешно сохранены в Google Sheets!")
        else:
            logger.error("Failed to save data to Google Sheets")
            await call.message.edit_text("Ошибка при сохранении данных в Google Sheets. Попробуйте снова позже.")
    except TelegramRetryAfter as e:
        logger.warning(f"Flood control on save, waiting {e.retry_after} seconds")
        await asyncio.sleep(e.retry_after + 1)
        if await append_row_to_sheet(recognized_data, call.from_user.id):
            await call.message.edit_text("Данные успешно сохранены в Google Sheets!")
        else:
            await call.message.edit_text("Ошибка при сохранении данных в Google Sheets. Попробуйте снова позже.")
    except Exception as e:
        logger.error(f"Error in handle_save_callback: {e}", exc_info=True)
        await call.message.edit_text("Произошла ошибка при сохранении.")
    
    await call.answer()

@photo_router.callback_query(BloodPressureCallback.filter(F.action == "edit"))
async def handle_edit_callback(call: CallbackQuery, callback_data: BloodPressureCallback, state: FSMContext) -> None:
    logger.info(f"Edit button clicked for field {callback_data.field} and data_id: {callback_data.data_id}")

    user_data = await state.get_data()
    recognized_data = user_data.get(callback_data.data_id)
    if not recognized_data:
        logger.warning(f"Data not found for data_id: {callback_data.data_id}")
        await call.answer("Данные не найдены. Попробуйте заново.", show_alert=True)
        return

    field = callback_data.field
    if field not in FIELD_PROMPTS:
        await call.answer("Неподдерживаемое поле.", show_alert=True)
        return

    await state.update_data({"editing_data_id": callback_data.data_id, "editing_field": field})
    await state.set_state(EditBPField.waiting_for_new_value)

    await call.message.edit_text(
        f"{FIELD_PROMPTS[field]}\nТекущее значение: {recognized_data.get(field, 'N/A')}"
    )
    await call.answer()

@photo_router.message(EditBPField.waiting_for_new_value)
async def process_edit_field(message: Message, state: FSMContext) -> None:
    state_data = await state.get_data()
    data_id = state_data.get("editing_data_id")
    field = state_data.get("editing_field")

    if not data_id or not field:
        await message.reply("Время ввода истекло. Повторите попытку.")
        await state.clear()
        return

    user_data = await state.get_data()
    recognized_data = user_data.get(data_id)
    if not recognized_data:
        await message.reply("Данные не найдены. Попробуйте заново.")
        await state.clear()
        return

    new_value = message.text.strip()
    if field in ("systolic", "diastolic", "pulse"):
        if not new_value.isdigit():
            await message.reply("Пожалуйста, введите целое число.")
            return
        new_value = int(new_value)
    elif field == "date":
        if new_value == "":
            new_value = None
        elif not re.match(r"^\d{4}-\d{2}-\d{2}$", new_value):
            await message.reply("Введите дату в формате YYYY-MM-DD.")
            return
    elif field == "time":
        if new_value == "":
            new_value = None
        elif not re.match(r"^\d{2}:\d{2}$", new_value):
            await message.reply("Введите время в формате HH:MM.")
            return

    recognized_data[field] = new_value if new_value != "" else None
    user_data[data_id] = recognized_data
    user_data.pop("editing_data_id", None)
    user_data.pop("editing_field", None)
    await state.set_data(user_data)
    await state.set_state(None)

    confirmation_text = build_confirmation_text(recognized_data)
    builder = build_confirmation_keyboard(data_id)
    await message.reply("Значение обновлено.", reply_markup=builder.as_markup())
    await message.reply(confirmation_text, reply_markup=builder.as_markup())

