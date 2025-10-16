import asyncio
import json
import logging
import os
from typing import Dict, Optional, Tuple

import aiofiles
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class UserDataManager:
    def __init__(self, filename: str = "user_data.json", bot_username: str = "") -> None:
        self.filename = filename
        self._file_lock = asyncio.Lock()
        self.bot_username = bot_username

    async def load_data(self) -> Dict[str, dict]:
        async with self._file_lock:
            try:
                async with aiofiles.open(self.filename, "r", encoding="utf-8") as file:
                    contents = await file.read()
                    return json.loads(contents)
            except FileNotFoundError:
                return {}
            except json.JSONDecodeError:
                logger.error(f"Ошибка чтения JSON: {self.filename}")
                return {}

    async def save_data(self, data: Dict[str, dict]) -> None:
        async with self._file_lock:
            async with aiofiles.open(self.filename, "w", encoding="utf-8") as file:
                json_str = json.dumps(data, indent=4, ensure_ascii=False)
                await file.write(json_str)

    async def get_or_create_user(
        self, user_id: int, username: str, referred_by: Optional[int] = None
    ) -> Dict[str, dict]:
        data = await self.load_data()
        uid = str(user_id)
        if uid not in data:
            data[uid] = {
                "username": username,
                "referrals": 0,
                "total_earned": 0.0,
                "referred_by": referred_by,
            }
            await self.save_data(data)
        return data[uid]

    async def add_referral(self, referrer_id: int) -> None:
        data = await self.load_data()
        ref_id = str(referrer_id)
        if ref_id in data:
            data[ref_id]["referrals"] += 1
            await self.save_data(data)

    async def get_user_stats(self, user_id: int) -> Tuple[int, float]:
        data = await self.load_data()
        uid = str(user_id)
        if uid in data:
            user = data[uid]
            return user["referrals"], user["total_earned"]
        return 0, 0.0

    def generate_referral_link(self, user_id: int) -> str:
        return f"https://t.me/{self.bot_username}?start=r{user_id}"

    async def find_user_by_username(self, username: str) -> Optional[str]:
        # username без @
        data = await self.load_data()
        for uid, user in data.items():
            if user.get("username") and user["username"].lower() == username.lower():
                return uid
        return None


class TelegramBotApp:
    def __init__(self) -> None:
        token = os.getenv("BOT_TOKEN")
        wallet = os.getenv("YOOMONEY_WALLET")

        if not token or not wallet:
            raise ValueError("Отсутствует BOT_TOKEN или YOOMONEY_WALLET в .env")

        self.token = token
        self.wallet = wallet
        self.user_data = UserDataManager()
        self.application = Application.builder().token(self.token).build()

        self._register_handlers()

    def _register_handlers(self) -> None:
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("menu", self.set_menu))
        self.application.add_handler(
            CallbackQueryHandler(self.purpose_handler, pattern="^purpose_")
        )
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input)
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        user_id = user.id
        username = user.username or user.first_name

        # Получаем username бота через get_me один раз и сохраняем
        me = await context.bot.get_me()
        self.user_data.bot_username = me.username

        args = context.args
        referred_by = None
        if args and args[0].startswith("r"):
            try:
                referred_by = int(args[0][1:])
            except ValueError:
                logger.warning(f"Некорректный рефкод: {args}")

        await self.user_data.get_or_create_user(user_id, username, referred_by)
        if referred_by:
            await self.user_data.add_referral(referred_by)

        text = (
            "✨ Добро пожаловать!\n"
            "🧸 Чтобы увидеть больше возможностей, используйте /menu.\n"
            "Можно приобрести Telegram звёзды без KYC и дешевле.\n\n"
            "🎁 Кому купить звёзды?"
        )

        keyboard = [
            [InlineKeyboardButton("Для себя", callback_data="purpose_self")],
            [InlineKeyboardButton("В подарок", callback_data="purpose_friend")],
        ]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    async def purpose_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        purpose = query.data

        if purpose == "purpose_self":
            context.user_data["awaiting_amount"] = True
            await query.edit_message_text("Введите сумму покупки (мин. 50):")

        elif purpose == "purpose_friend":
            context.user_data["awaiting_friend_username"] = True
            await query.edit_message_text("Введите @username друга:")

    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text.strip()

        if context.user_data.get("awaiting_friend_username"):
            if not text.startswith("@"):
                await update.message.reply_text("Введите username с @. Пример: @username")
                return

            username = text[1:]  # убираем @
            friend_id = await self.user_data.find_user_by_username(username)
            if not friend_id:
                ref_link = self.user_data.generate_referral_link(update.effective_user.id)
                await update.message.reply_text(
                    "❌ Пользователь не найден. "
                    "Для того чтобы вы могли подарить звёзды, этот пользователь должен сначала запустить бота по вашей реферальной ссылке:\n\n"
                    f"<code>{ref_link}</code>\n"
                    "Отправьте эту ссылку другу, чтобы он открыл бота и вы смогли продолжить покупку.",
                    parse_mode="HTML",
                )
                return

            context.user_data["friend_username"] = text
            context.user_data.pop("awaiting_friend_username")
            context.user_data["awaiting_amount"] = True
            await update.message.reply_text("✅ Найден! Введите сумму (мин. 50):")
            return

        if context.user_data.get("awaiting_amount"):
            try:
                amount = int(text)
            except ValueError:
                await update.message.reply_text("Введите корректное число.")
                return

            if amount < 50 or amount > 1_000_000:
                await update.message.reply_text("Диапазон: 50–1,000,000 звёзд.")
                return

            context.user_data.pop("awaiting_amount")
            user_id = update.effective_user.id
            price_rub = max(2, round(amount * 0.05, 2))
            comment = f"Stars_{amount}_uid{user_id}"
            url = f"https://yoomoney.ru/to/{self.wallet}?amount={price_rub}&comment={comment}"
            keyboard = [[InlineKeyboardButton("СБП (YooMoney)", url=url)]]
            await update.message.reply_text(
                "⭐ Выберите способ оплаты (счёт действителен 30 мин.):",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif text == "⭐ Купить Звезды":
            keyboard = [
                [InlineKeyboardButton("Для себя", callback_data="purpose_self")],
                [InlineKeyboardButton("В подарок", callback_data="purpose_friend")],
            ]
            await update.message.reply_text(
                "🎁 Кому купить звёзды:", reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif text == "👥 Реферальная система":
            await self.partner_program(update, context)

    async def partner_program(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        ref, earned = await self.user_data.get_user_stats(user_id)
        link = self.user_data.generate_referral_link(user_id)
        text = (
            "👥 <b>Реферальная система</b>\n"
            "✨ Зарабатывайте 10% от расходов приглашённых!\n\n"
            f"<b>🔗 Ваша ссылка:</b>\n<code>{link}</code>\n\n"
            f"👥 Рефералов: <b>{ref}</b>\n"
            f"💸 Заработано: <b>{earned:.2f} RUB</b>"
        )
        keyboard = [
            [InlineKeyboardButton("📤 Поделиться", switch_inline_query=f"Покупайте звёзды: {link}")],
        ]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    async def set_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = [["⭐ Купить Звезды"], ["👥 Реферальная система"]]
        await update.message.reply_text(
            "Главное меню:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )

    def run(self) -> None:
        logger.info("✅ Бот запущен")
        self.application.run_polling()


if __name__ == "__main__":
    bot = TelegramBotApp()
    bot.run()
