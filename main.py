import asyncio
import logging
import random
import re
import os
import json
import pickle
from typing import Dict, Any, Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, \
    CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType

from thefuzz import process, fuzz
import google.generativeai as genai

# --- КОНФИГУРАЦИЯ ---
TOKEN = "ВАШ ТОКЕН БОТА (bot_father)"
GEMINI_KEY = "ВАШ API КЛЮЧ"

genai.configure(api_key=GEMINI_KEY.strip())
model = genai.GenerativeModel('gemini-2.5-flash')

logging.basicConfig(level=logging.INFO)


class PickleFileStorage(BaseStorage):

    def __init__(self, path: str = "bot_state.pkl"):
        self.path = path
        self.data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            logging.error(f"Error loading storage: {e}")
            return {}

    def _save(self):
        try:
            with open(self.path, "wb") as f:
                pickle.dump(self.data, f)
        except Exception as e:
            logging.error(f"Error saving storage: {e}")

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        self.data.setdefault(key.user_id, {})["state"] = state.state if state else None
        self._save()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        return self.data.get(key.user_id, {}).get("state")

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        self.data.setdefault(key.user_id, {})["data"] = data
        self._save()

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        return self.data.get(key.user_id, {}).get("data", {})

    async def close(self) -> None:
        pass


HIGHSCORES_FILE = "highscores.json"


def get_high_score(user_id: int) -> int:
    if not os.path.exists(HIGHSCORES_FILE):
        return 0
    try:
        with open(HIGHSCORES_FILE, "r", encoding="utf-8") as f:
            scores = json.load(f)
            return scores.get(str(user_id), 0)
    except:
        return 0


def save_high_score(user_id: int, score: int):
    scores = {}
    if os.path.exists(HIGHSCORES_FILE):
        try:
            with open(HIGHSCORES_FILE, "r", encoding="utf-8") as f:
                scores = json.load(f)
        except:
            scores = {}

    current_high = scores.get(str(user_id), 0)
    if score > current_high:
        scores[str(user_id)] = score
        with open(HIGHSCORES_FILE, "w", encoding="utf-8") as f:
            json.dump(scores, f)
        return True
    return False


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (Нормализация) ---
def normalize_city(city_name: str) -> str:
    return re.sub(r'[^a-zа-яё]', '', city_name.lower())


def get_last_valid_char(city_name: str) -> str:
    normalized = normalize_city(city_name)
    bad_chars = {'ь', 'ъ', 'ы', 'й'}
    for char in reversed(normalized):
        if char not in bad_chars:
            return char
    return normalized[-1]


def get_penultimate_valid_char(city_name: str) -> Optional[str]:
    """Возвращает предпоследнюю валидную букву для механики спасения"""
    normalized = normalize_city(city_name)
    bad_chars = {'ь', 'ъ', 'ы', 'й'}

    # 1. Находим индекс последней игровой буквы
    last_idx = -1
    for i in range(len(normalized) - 1, -1, -1):
        if normalized[i] not in bad_chars:
            last_idx = i
            break

    if last_idx <= 0:
        return None

    # 2. Ищем букву перед ней
    for i in range(last_idx - 1, -1, -1):
        if normalized[i] not in bad_chars:
            return normalized[i]

    return None


# ЗАГРУЗКА ГОРОДОВ
RAW_CITIES = []
if os.path.exists("cities.txt"):
    with open("cities.txt", "r", encoding="utf-8") as f:
        RAW_CITIES = [city.strip() for city in f if city.strip()]
else:
    logging.warning("Файл cities.txt не найден! Бот не будет знать городов.")

CITIES_MAP = {normalize_city(c): c for c in RAW_CITIES}
CITIES_KEYS = list(CITIES_MAP.keys())

TOP_CITIES_NORM = set()
if os.path.exists("top_cities.txt"):
    with open("top_cities.txt", "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                TOP_CITIES_NORM.add(normalize_city(line.strip()))
else:
    logging.warning("Файл top_cities.txt не найден. Приоритезация работать не будет.")


def find_best_match(user_input: str, threshold: int = 72):
    user_norm = normalize_city(user_input)
    if user_norm in CITIES_MAP:
        return user_norm, CITIES_MAP[user_norm]

    result = process.extractOne(user_norm, CITIES_KEYS, scorer=fuzz.ratio)
    if result:
        best_match, score = result
        if score >= threshold:
            return best_match, CITIES_MAP[best_match]
    return None


# ФУНКЦИЯ ЗАПРОСА К AI
async def get_cities_info(user_city: str, bot_city: str) -> str:
    prompt = (
        f"Напиши интересную справку о двух городах: {user_city} и {bot_city}. "
        f"Формат ответа должен быть строго таким:\n\n"
        f"<b>🌍 Подробнее о городе {user_city}:</b>\n\n"
        f"(Тут 3-4 предложения: страна, население, интересный факт)\n\n"
        f"<b>🌍 Подробнее о городе {bot_city}:</b>\n\n"
        f"(Тут 3-4 предложения: страна, население, интересный факт)\n\n"
        f"В начале каждого факта - эмодзи-флаг страны, к которой принадлежит город. Пиши по-русски, используй эмодзи, будь краток."
        f"Описывай только реально существующие города, если не знаешь - лучше честно ответь что информацию не нашел.")
    try:
        response = await model.generate_content_async(prompt)
        return response.text
    except Exception as e:
        logging.error(f"Ошибка Gemini: {e}")
        return "\n⚠️ Не удалось загрузить факты (ошибка ИИ), но города верные!"


# --- БОТ ---

class GameState(StatesGroup):
    playing = State()


game_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="💡 Подсказка"), KeyboardButton(text="🔄 Закончить текущую игру")]],
    resize_keyboard=True
)

dp = Dispatcher(storage=PickleFileStorage("bot_state.pkl"))


@dp.message(Command("start"))
async def command_start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(GameState.playing)
    await state.update_data(used_cities=[], last_letter=None, penult_letter=None, score=0)
    await message.answer("""Привет! Давай играть в Города?🌏
Напиши название любого города, а я не только отвечу тебе, но и подберу к нему интересные факты с помощью ИИ🤖""",
                         reply_markup=game_kb)


@dp.message(Command("stop"), GameState.playing)
@dp.message(F.text == "🔄 Закончить текущую игру", GameState.playing)
async def stop_game(message: Message, state: FSMContext):
    data = await state.get_data()
    current_score = data.get("score", 0)
    user_id = message.from_user.id

    is_new_record = save_high_score(user_id, current_score)
    high_score = get_high_score(user_id)

    text = f"Игра окончена! Твой счет: <b>{current_score}</b>.\nЛучший рекорд: <b>{high_score}</b> 🏆"
    if is_new_record:
        text += "\n🎉 Поздравляю! Это новый рекорд!"

    await message.answer(text, parse_mode="HTML", reply_markup=types.ReplyKeyboardRemove())

    await state.clear()
    await state.set_state(GameState.playing)
    await state.update_data(used_cities=[], last_letter=None, penult_letter=None, score=0)
    await message.answer("🔄 Игра перезапущена! Напиши любой город, чтобы начать заново☺️", reply_markup=game_kb)


@dp.message(F.text == "💡 Подсказка", GameState.playing)
async def give_hint(message: Message, state: FSMContext):
    data = await state.get_data()
    last_letter = data.get("last_letter")
    used_cities = set(data.get("used_cities", []))

    if not last_letter:
        await message.answer("Твой ход, назови любой город☺️")
        return

    # Логика поиска подсказки (сначала в топ, потом везде)
    top_candidates = [
        CITIES_MAP[norm] for norm in CITIES_MAP
        if norm.startswith(last_letter) and norm not in used_cities and norm in TOP_CITIES_NORM
    ]

    candidates = top_candidates if top_candidates else [
        CITIES_MAP[norm] for norm in CITIES_MAP
        if norm.startswith(last_letter) and norm not in used_cities
    ]

    if not candidates:
        # Если городов на основную букву нет, пробуем найти на предпоследнюю (если правило активно)
        penult = data.get("penult_letter")
        if penult:
            candidates = [
                CITIES_MAP[norm] for norm in CITIES_MAP
                if norm.startswith(penult) and norm not in used_cities
            ]

    if not candidates:
        await message.answer("Города кончились совсем! Ты победил!🏆")
        await state.clear()
    else:
        await message.answer(f"Попробуй: <code>{random.choice(candidates)}</code> 🤫", parse_mode="HTML")


# ЛОГИКА ИГРЫ
@dp.message(GameState.playing)
async def play_game(message: Message, state: FSMContext):
    user_text = message.text.strip()

    match_result = find_best_match(user_text)
    if not match_result:
        await message.answer(
            "Не знаю такого города или опечатка сильная 🤷‍♂️\nЕсли застрял, то ты всегда можешь воспользоваться подсказкой👇")
        return

    found_norm, found_real = match_result

    if normalize_city(user_text) != found_norm:
        await message.answer(f"Города <b>{user_text}</b> я не знаю, но думаю, ты имел в виду: <b>{found_real}</b>?",
                             parse_mode="HTML")

    # ПРОВЕРКА ПРАВИЛ
    data = await state.get_data()
    used_cities = set(data.get("used_cities", []))
    expected_letter = data.get("last_letter")
    expected_penult = data.get("penult_letter")
    current_score = data.get("score", 0)

    if found_norm in used_cities:
        await message.answer(
            f"Город {found_real} уже был, попробуй другой🙏\nТакже ты можешь воспользоваться подсказкой👇")
        return

    # Логика валидации первой буквы с учетом "спасения"
    if expected_letter:
        is_primary_ok = found_norm.startswith(expected_letter)
        is_penult_ok = expected_penult and found_norm.startswith(expected_penult)

        if not is_primary_ok:
            # Если буква не основная, проверяем, можно ли использовать предпоследнюю
            allowed_rescue = False
            if is_penult_ok:
                # Проверяем, действительно ли кончились города на основную букву
                remaining_on_primary = [
                    c for c in CITIES_MAP
                    if c.startswith(expected_letter) and c not in used_cities
                ]
                if not remaining_on_primary:
                    allowed_rescue = True
                    await message.answer(
                        f"Города на <b>'{expected_letter.upper()}'</b> закончились. Принимаю ответ на предпоследнюю букву <b>'{expected_penult.upper()}'</b>! 🤝",
                        parse_mode="HTML"
                    )
                else:
                    await message.answer(
                        f"Рано сдаешься! Города на букву <b>'{expected_letter.upper()}'</b> еще есть 😉. Я знаю как минимум один.",
                        parse_mode="HTML"
                    )
                    return  # Не пускаем дальше

            if not allowed_rescue:
                await message.answer(
                    f"Нужно на букву <b>{expected_letter.upper()}</b>!\nЕсли застрял, то ты всегда можешь воспользоваться подсказкой👇",
                    parse_mode="HTML")
                return

    # 3. Ход пользователя принят
    current_used = data.get("used_cities", [])
    current_used.append(found_norm)
    current_score += 1

    await state.update_data(used_cities=current_used, score=current_score)

    # 4. Логика ответа бота
    last_char_for_bot = get_last_valid_char(found_real)

    await make_bot_move(message, state, last_char_for_bot, found_real)


# ХОД БОТА
async def make_bot_move(message: Message, state: FSMContext, letter: str, user_real_city: str):
    data = await state.get_data()
    current_used = data.get("used_cities", [])
    current_score = data.get("score", 0)

    all_candidates_norm = [
        norm for norm in CITIES_MAP
        if norm.startswith(letter) and norm not in current_used
    ]

    # ЕСЛИ У БОТА НЕТ ГОРОДОВ
    if not all_candidates_norm:
        penultimate_char = get_penultimate_valid_char(user_real_city)

        can_continue = False
        if penultimate_char:
            check_candidates = [n for n in CITIES_MAP if n.startswith(penultimate_char) and n not in current_used]
            if check_candidates:
                can_continue = True

        if can_continue:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"▶️ Продолжить на '{penultimate_char.upper()}'",
                                      callback_data=f"cont_{penultimate_char}")],
                [InlineKeyboardButton(text="🏳️ Забрать победу", callback_data="stop_win")]
            ])
            await message.answer(
                f"Ого! Ты назвал <b>{user_real_city}</b>. Города на букву <b>'{letter.upper()}'</b> у меня закончились! 🤯\n\n"
                f"Ты можешь закончить игру победителем или дать мне шанс отыграться на предпоследнюю букву (<b>'{penultimate_char.upper()}'</b>).",
                reply_markup=kb, parse_mode="HTML"
            )
            return
        else:
            save_high_score(message.from_user.id, current_score)
            await message.answer(
                f"Ты назвал <b>{user_real_city}</b>. Мне нечем ответить ни на '{letter.upper()}', ни на предпоследнюю букву. Абсолютная победа! 🏆\n"
                f"Твой итоговый счет: {current_score}", parse_mode="HTML")
            await state.clear()
            return

    # ВЫБОР ГОРОДА
    top_candidates = [norm for norm in all_candidates_norm if norm in TOP_CITIES_NORM]

    if top_candidates:
        bot_norm = random.choice(top_candidates)
    else:
        bot_norm = random.choice(all_candidates_norm)

    bot_answer = CITIES_MAP[bot_norm]
    current_used.append(bot_norm)

    next_letter_for_user = get_last_valid_char(bot_answer)
    next_penult_for_user = get_penultimate_valid_char(bot_answer)

    await state.update_data(
        used_cities=current_used,
        last_letter=next_letter_for_user,
        penult_letter=next_penult_for_user
    )

    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Подробнее о городах от ИИ", callback_data="get_facts")]
    ])

    response_text = (
        f"🫡 Принято: <b>{user_real_city}</b>\n\n"
        f"🤔 Мой ответ: <b>{bot_answer}</b>\n\n"
        f"👉 Тебе на букву: <b>{next_letter_for_user.upper()}</b>\n"
        f"<i>(Счет: {current_score})</i>"
    )

    await message.answer(response_text, parse_mode="HTML", reply_markup=inline_kb)


# ОБРАБОТЧИКИ КНОПОК

@dp.callback_query(F.data == "stop_win")
async def stop_win_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    score = data.get("score", 0)
    save_high_score(callback.from_user.id, score)

    await callback.message.edit_text(
        f"Ты решил забрать кубок! 🏆\nФинальный счет: <b>{score}</b>.\nМожешь начать новую игру /start",
        parse_mode="HTML"
    )
    await state.clear()


@dp.callback_query(F.data.startswith("cont_"))
async def continue_game_handler(callback: CallbackQuery, state: FSMContext):
    new_letter = callback.data.split("_")[1]

    text = callback.message.text or ""
    match = re.search(r"Ты назвал (.+)\.", text)
    user_city_real = match.group(1).strip() if match else "Твой город"

    await callback.message.edit_text(f"🤝 Благородно! Продолжаем на букву <b>{new_letter.upper()}</b>...",
                                     parse_mode="HTML")
    await make_bot_move(callback.message, state, new_letter, user_city_real)


@dp.callback_query(F.data == "get_facts")
async def facts_callback_handler(callback: CallbackQuery):
    await callback.answer("Ждем ответа нейросети...🤗", show_alert=False)

    text = callback.message.text

    match_user = re.search(r"Принято:\s+(.+)", text)
    match_bot = re.search(r"Мой ответ:\s+(.+)", text)

    if match_user and match_bot:
        city_1 = match_user.group(1).strip()
        city_2 = match_bot.group(1).strip()

        await callback.message.bot.send_chat_action(chat_id=callback.message.chat.id, action="typing")

        ai_facts = await get_cities_info(city_1, city_2)

        original_html = callback.message.html_text
        new_text = f"{original_html}\n\n{ai_facts}"

        await callback.message.edit_text(new_text, parse_mode="HTML", reply_markup=None)
    else:
        await callback.answer("Не удалось определить города :(", show_alert=True)


async def main() -> None:
    bot = Bot(token=TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
