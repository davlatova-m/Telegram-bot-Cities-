import asyncio
import logging
import random
import re

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# библиотека для нечеткого сравнения
from thefuzz import process, fuzz

TOKEN = "your bot token"

logging.basicConfig(level=logging.INFO)

with open("cities.txt", "r", encoding="utf-8") as f:
    RAW_CITIES = [city.strip() for city in f if city.strip()]


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def normalize_city(city_name: str) -> str:
    """Убирает всё лишнее, приводит к нижнему регистру."""
    return re.sub(r'[^a-zа-яё]', '', city_name.lower())


def get_last_valid_char(city_name: str) -> str:
    """Возвращает последнюю значимую букву (пропуская ь, ъ, ы)."""
    normalized = normalize_city(city_name)
    bad_chars = {'ь', 'ъ', 'ы', 'й'}
    for char in reversed(normalized):
        if char not in bad_chars:
            return char
    return normalized[-1]


# Словарь: {нормализованное_имя: Красивое Имя}
CITIES_MAP = {normalize_city(c): c for c in RAW_CITIES}
# Список только ключей для поиска (нормализованных имен)
CITIES_KEYS = list(CITIES_MAP.keys())


def find_best_match(user_input: str, threshold: int = 75):
    """
    Ищет город в списке, допуская опечатки, сравнивая слова целиком.
    """
    user_norm = normalize_city(user_input)

    # 1. Прямое совпадение (самое надежное)
    if user_norm in CITIES_MAP:
        return user_norm, CITIES_MAP[user_norm]

    # 2. Нечеткий поиск с scorer=fuzz.ratio. fuzz.ratio сравнивает строки целиком
    result = process.extractOne(user_norm, CITIES_KEYS, scorer=fuzz.ratio)

    if result:
        best_match, score = result
        if score >= threshold:
            return best_match, CITIES_MAP[best_match]

    return None


# --- FSM и КЛАВИАТУРА ---

class GameState(StatesGroup):
    playing = State()


game_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="💡 Подсказка"), KeyboardButton(text="⛔ Сдаюсь")]],
    resize_keyboard=True
)

dp = Dispatcher(storage=MemoryStorage())


# --- ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def command_start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(GameState.playing)
    await state.update_data(used_cities=[], last_letter=None)
    await message.answer("Привет! Давай играть в Города. Напиши любой город!", reply_markup=game_kb)


@dp.message(Command("stop"), GameState.playing)
@dp.message(F.text == "⛔ Сдаюсь", GameState.playing)
async def stop_game(message: Message, state: FSMContext):
    await message.answer("Игра окончена! Жми /start", reply_markup=types.ReplyKeyboardRemove())
    await state.clear()


@dp.message(F.text == "💡 Подсказка", GameState.playing)
async def give_hint(message: Message, state: FSMContext):
    data = await state.get_data()
    last_letter = data.get("last_letter")
    used_cities = set(data.get("used_cities", []))

    if not last_letter:
        await message.answer("Назови любой город!")
        return

    candidates = [
        real for norm, real in CITIES_MAP.items()
        if norm.startswith(last_letter) and norm not in used_cities
    ]

    if not candidates:
        await message.answer("Города кончились, ты победил! /start")
        await state.clear()
    else:
        await message.answer(f"Попробуй: <b>{random.choice(candidates)}</b>", parse_mode="HTML")


@dp.message(GameState.playing)
async def play_game(message: Message, state: FSMContext):
    user_text = message.text.strip()

    # --- 1. ПОИСК ГОРОДА С УЧЕТОМ ОПЕЧАТОК ---
    match_result = find_best_match(user_text)

    if not match_result:
        await message.answer("Я не знаю такого города (или слишком много опечаток) 🤷‍♂️")
        return

    # found_norm - ключ (нормализованный), found_real - красивое название
    found_norm, found_real = match_result

    # Если была опечатка, скажем пользователю, что мы его поняли
    # Сравниваем исходный текст (в нижнем регистре) с найденным
    if normalize_city(user_text) != found_norm:
        await message.answer(f"Думаю, ты имел в виду: <b>{found_real}</b>", parse_mode="HTML")

    data = await state.get_data()
    used_cities = set(data.get("used_cities", []))
    expected_letter = data.get("last_letter")

    # --- 2. ПРОВЕРКИ ПРАВИЛ ИГРЫ ---

    # А. Не повторялся ли город
    if found_norm in used_cities:
        await message.answer(f"Город {found_real} уже был! Вспоминай другой.")
        return

    # Б. Правильная ли буква (важно проверять именно по найденному городу, а не по опечатке юзера)
    if expected_letter:
        if not found_norm.startswith(expected_letter):
            await message.answer(f"Нужно назвать город на букву <b>{expected_letter.upper()}</b>!", parse_mode="HTML")
            return

    # --- ХОД ПРИНЯТ ---
    current_used = data.get("used_cities", [])
    current_used.append(found_norm)

    # Вычисляем последнюю букву из ПРАВИЛЬНОГО названия города
    last_char_for_bot = get_last_valid_char(found_real)

    # --- ОТВЕТ БОТА ---
    candidates = [
        real for norm, real in CITIES_MAP.items()
        if norm.startswith(last_char_for_bot) and norm not in current_used
    ]

    if not candidates:
        await message.answer(
            f"Ты назвал {found_real}.\nМне нечем крыть на '{last_char_for_bot.upper()}'. Ты выиграл! 🏆")
        await state.clear()
        return

    bot_answer = random.choice(candidates)
    bot_norm = normalize_city(bot_answer)
    current_used.append(bot_norm)

    next_letter_for_user = get_last_valid_char(bot_answer)

    await state.update_data(used_cities=current_used, last_letter=next_letter_for_user)

    await message.answer(
        f"Принято: {found_real}.\n"
        f"Мой ответ: <b>{bot_answer}</b>.\n"
        f"Тебе на букву: <b>{next_letter_for_user.upper()}</b>",
        parse_mode="HTML"
    )


async def main() -> None:
    bot = Bot(token=TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())