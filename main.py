
import asyncio
import logging
import os
from datetime import datetime, timedelta
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, FloodWait

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Конфиг из env (для Railway)
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
DB_PATH = "sessions.db"

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# FSM для авторизации
class AuthStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()

class SendStates(StatesGroup):
    waiting_target = State()
    waiting_text = State()
    waiting_interval = State()
    waiting_count = State()

# База данных
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (user_id INTEGER PRIMARY KEY, 
                  session_string TEXT, 
                  phone TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks 
                 (task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  target TEXT,
                  text TEXT,
                  interval INTEGER,
                  count INTEGER,
                  status TEXT)''')
    conn.commit()
    conn.close()

init_db()

async def get_client(user_id: int) -> Client | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_string FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        return Client(
            name=f"session_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=row[0],
            in_memory=True
        )
    return None

# Команда старт
@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    client = await get_client(user_id)
    if client:
        await message.answer("✅ Аккаунт уже авторизован. Используйте /panel")
    else:
        await message.answer("Введите номер телефона в формате +1234567890:")
        await state.set_state(AuthStates.waiting_phone)

# Авторизация
@dp.message(AuthStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(phone=phone)
    
    client = Client(
        name=f"temp_{message.from_user.id}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True
    )
    
    await client.connect()
    try:
        sent_code = await client.send_code(phone)
        await state.update_data(client=client, phone_code_hash=sent_code.phone_code_hash)
        await message.answer("Введите код из SMS/Telegram:")
        await state.set_state(AuthStates.waiting_code)
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")
        await state.clear()

@dp.message(AuthStates.waiting_code)
async def process_code(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    client: Client = data['client']
    phone = data['phone']
    phone_code_hash = data['phone_code_hash']
    
    try:
        await client.sign_in(phone, phone_code_hash, code)
        session_string = await client.export_session_string()
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO users (user_id, session_string, phone) VALUES (?, ?, ?)",
                  (message.from_user.id, session_string, phone))
        conn.commit()
        conn.close()
        
        await message.answer("✅ Успешный вход! Используйте /panel")
        await state.clear()
    except SessionPasswordNeeded:
        await message.answer("Введите 2FA пароль:")
        await state.set_state(AuthStates.waiting_password)
        await state.update_data(client=client, phone=phone)
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")
        await state.clear()

@dp.message(AuthStates.waiting_password)
async def process_password(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    client: Client = data['client']
    
    try:
        await client.check_password(password)
        session_string = await client.export_session_string()
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO users (user_id, session_string, phone) VALUES (?, ?, ?)",
                  (message.from_user.id, session_string, data['phone']))
        conn.commit()
        conn.close()
        
        await message.answer("✅ Успешный вход с 2FA!")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")
    finally:
        await state.clear()

# Панель управления
@dp.message(Command("panel"))
async def panel(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Новая рассылка", callback_data="new_send")],
        [InlineKeyboardButton(text="📋 Мои задачи", callback_data="my_tasks")],
        [InlineKeyboardButton(text="🔄 Перелогиниться", callback_data="relogin")]
    ])
    await message.answer("Панель управления:", reply_markup=keyboard)

@dp.callback_query(F.data == "new_send")
async def new_send(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите ID или username получателя (можно несколько через запятую):")
    await state.set_state(SendStates.waiting_target)
    await callback.answer()

@dp.message(SendStates.waiting_target)
async def process_target(message: Message, state: FSMContext):
    targets = [t.strip() for t in message.text.split(',')]
    await state.update_data(targets=targets)
    await message.answer("Введите текст сообщения:")
    await state.set_state(SendStates.waiting_text)

@dp.message(SendStates.waiting_text)
async def process_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    await message.answer("Интервал между сообщениями в секундах (минимум 5):")
    await state.set_state(SendStates.waiting_interval)

@dp.message(SendStates.waiting_interval)
async def process_interval(message: Message, state: FSMContext):
    try:
        interval = int(message.text)
        if interval < 5:
            raise ValueError
        await state.update_data(interval=interval)
        await message.answer("Сколько сообщений отправить? (0 = бесконечно, осторожно!):")
        await state.set_state(SendStates.waiting_count)
    except:
        await message.answer("Введите корректное число >=5")

@dp.message(SendStates.waiting_count)
async def process_count(message: Message, state: FSMContext):
    try:
        count = int(message.text)
        data = await state.get_data()
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""INSERT INTO tasks 
                     (user_id, target, text, interval, count, status) 
                     VALUES (?, ?, ?, ?, ?, 'pending')""",
                  (message.from_user.id, ','.join(data['targets']), data['text'], data['interval'], count))
        task_id = c.lastrowid
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ Задача #{task_id} создана! Запускаю...")
        asyncio.create_task(run_task(message.from_user.id, task_id))
        await state.clear()
    except:
        await message.answer("Ошибка в количестве")

async def run_task(user_id: int, task_id: int):
    client = await get_client(user_id)
    if not client:
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    task = c.fetchone()
    conn.close()
    
    if not task:
        return
    
    targets = task[2].split(',')
    text = task[3]
    interval = task[4]
    count = task[5]
    
    async with client:
        sent = 0
        while (count == 0 or sent < count):
            for target in targets:
                try:
                    await client.send_message(target, text)
                    sent += 1
                    await asyncio.sleep(interval)
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                except Exception as e:
                    logging.error(f"Error sending: {e}")
                    await asyncio.sleep(10)

# Запуск
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.ru
