import asyncio
import json
import logging
import os
from typing import Dict, Optional, Tuple
from urllib.parse import quote

import aiofiles
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class UserDataManager:
    def __init__(
        self, filename: str = "user_data.json", bot_username: str = ""
    ) -> None:
        self._filename = filename
        # Теперь bot_username передаётся конструктором из TelegramBotApp
        self._bot_username = bot_username
        self._file_lock = asyncio.Lock()

    async def load_data(self) -> Dict[str, dict]:
        async with self._file_lock:
            try:
                async with aiofiles.open(self._filename, mode="r", encoding="utf-8") as file:
                    contents = await file.read()
                    return json.loads(contents)
            except FileNotFoundError:
                return {}
            except json.JSONDecodeError:
                logger.error(f"Ошибка декодирования JSON: {self._filename}")
                return {}

    async def save_data(self, data: Dict[str, dict]) -> None:
        async with self._file_lock:
            async with aiofiles.open(self._filename, mode="w", encoding="utf-8") as file:
                json_str = json.dumps(data, indent=4, ensure_ascii=False)
                await file.write(json_str)

    async def get_or_create_user(
        self, user_id: int, username: str, referred_by: Optional[int] = None
    ) -> Dict[str, dict]:
        data = await self.load_data()
        user_key = str(user_id)
        if user_key not in data:
            data[user_key] = {
                "username": username,
                "referrals": 0,
                "total_earned": 0.0,
                "balance_stars": 0,
                "referred_by": referred_by,
            }
            await self.save_data(data)
        return data[user_key]

    async def add_user_balance(self, user_id: int, amount: int) -> None:
        data = await self.load_data()
        user_key = str(user_id)
        if user_key in data:
            data[user_key].setdefault("balance_stars", 0)
            data[user_key]["balance_stars"] += amount
            await self.save_data(data)
            logger.info(f"Баланс пользователя {user_id} пополнен на {amount} звёзд.")

    async def get_user_stats(self, user_id: int) -> Tuple[int, float]:
        data = await self.load_data()
        user_key = str(user_id)
        if user_key in data:
            user = data[user_key]
            return user.get("referrals", 0), user.get("total_earned", 0.0)
        return 0, 0.0

    # Теперь метод использует self._bot_username, установленный из .env
    def generate_referral_link(self, user_id: int) -> str:
        return f"https://t.me/{self._bot_username}?start=r{user_id}"

    async def find_user_by_username(self, username: str) -> Optional[str]:
        data = await self.load_data()
        username_lower = username.lower()
        for user_id, user_info in data.items():
            if user_info.get("username", "").lower() == username_lower:
                return user_id
        return None


class TelegramBotApp:
    def __init__(self) -> None:
        token = os.getenv("BOT_TOKEN")
        wallet = os.getenv("YOOMONEY_WALLET")
        admin_chat_id = os.getenv("ADMIN_CHAT_ID")
        star_rate_str = os.getenv("STAR_RATE")
        support_username = os.getenv("SUPPORT_USERNAME")
        # --- Новая переменная ---
        bot_username_from_env = os.getenv("BOT_USERNAME_FOR_LINK") # <--- Имя бота для ссылки из .env
        # ------------------------

        if not all([token, wallet, admin_chat_id, star_rate_str, support_username, bot_username_from_env]): # <--- Добавлена проверка
            raise ValueError(
                "Одна из переменных окружения не задана: BOT_TOKEN, YOOMONEY_WALLET, ADMIN_CHAT_ID, STAR_RATE, SUPPORT_USERNAME, BOT_USERNAME_FOR_LINK"
            )

        try:
            self._star_rate = float(star_rate_str)
        except ValueError:
            raise ValueError("Переменная STAR_RATE должна быть числом (например, 1 звезда = 1.3 рубля)")

        self._token = token
        self._wallet = wallet
        self._admin_chat_id = int(admin_chat_id)
        self._support_username = support_username.lstrip('@')
        # --- Передаём имя бота из .env в UserDataManager ---
        self._user_data_manager = UserDataManager(bot_username=bot_username_from_env)
        # ---------------------------------------------------
        self._application = Application.builder().token(self._token).build()

        self._register_handlers()

    def _register_handlers(self) -> None:
        self._application.add_handler(CommandHandler("start", self._start))
        self._application.add_handler(CommandHandler("menu", self._set_menu))
        self._application.add_handler(CallbackQueryHandler(self._purpose_handler, pattern="^purpose_"))
        self._application.add_handler(CallbackQueryHandler(self._payment_confirm_handler, pattern="^confirm_payment"))
        self._application.add_handler(CallbackQueryHandler(self._admin_action_handler, pattern="^admin_"))
        self._application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_input))

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user:
            return

        # me = await context.bot.get_me() # <--- Больше не нужно получать username динамически для ссылки
        # self._user_data_manager._bot_username = me.username # <--- Убрано

        referred_by = None
        if context.args and context.args[0].startswith('r'):
            try:
                referred_by = int(context.args[0][1:])
            except (ValueError, IndexError):
                referred_by = None
        
        await self._user_data_manager.get_or_create_user(
            user.id, user.username or user.first_name, referred_by
        )
        
        await self._set_menu(update, context)

    async def _handle_amount_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> None:
        try:
            amount = int(text)
        except ValueError:
            await update.message.reply_text("Пожалуйста, введите корректное число.")
            return

        if not (50 <= amount <= 1_000_000):
            await update.message.reply_text("Сумма должна быть в диапазоне от 50 до 1,000,000.")
            return

        user_id = update.effective_user.id
        price_rub = max(2, round(amount * self._star_rate, 2))
        comment = f"Stars_{amount}_uid{user_id}"

        context.user_data["payment_amount"] = amount
        context.user_data["payment_price"] = price_rub
        context.user_data["payment_comment"] = comment
        context.user_data.pop("awaiting_amount", None)

        encoded_comment = quote(comment)
        url = f"https://yoomoney.ru/to/{self._wallet}?amount={price_rub}&comment={encoded_comment}"
        keyboard = [
            [InlineKeyboardButton("Оплатить через YooMoney (СБП)", url=url)],
            [InlineKeyboardButton("✅ Я оплатил", callback_data="confirm_payment")]
        ]
        await update.message.reply_text(
            "⭐ Выберите способ оплаты (счёт действителен 30 мин.):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _payment_confirm_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = query.from_user
        
        amount = context.user_data.get("payment_amount")
        price = context.user_data.get("payment_price")
        comment = context.user_data.get("payment_comment")
        friend_username = context.user_data.get("friend_username")

        if not all([amount, price, comment]):
            await query.answer("❗️Не удалось найти данные о платеже. Попробуйте снова.", show_alert=True)
            return

        await query.answer()
        await query.edit_message_text("Спасибо! Ваш платёж проверяется. Обычно это занимает несколько минут.")

        payer_username = f"@{user.username}" if user.username else user.full_name
        
        admin_text = (
            f"🔔 Новый запрос!\n\n"
            f"👤 **Отправитель:** {payer_username} (ID: `{user.id}`)\n"
        )
        if friend_username:
            admin_text += f"🎁 **Для получателя:** `@{friend_username}`\n"
        admin_text += (
            f"⭐️ **Кол-во звёзд:** {amount}\n"
            f"💰 **Сумма:** {price} RUB\n"
            f"📝 **Комментарий к платежу:** `{comment}`"
        )

        if friend_username:
            callback_data_confirm = f"admin_confirm_gift_{user.id}_{amount}_{friend_username}"
        else:
            callback_data_confirm = f"admin_confirm_self_{user.id}_{amount}"
        callback_data_decline = f"admin_decline_{user.id}"
        
        admin_keyboard = [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=callback_data_confirm),
                InlineKeyboardButton("❌ Отклонить", callback_data=callback_data_decline)
            ]
        ]
        await context.bot.send_message(
            chat_id=self._admin_chat_id,
            text=admin_text,
            reply_markup=InlineKeyboardMarkup(admin_keyboard),
            parse_mode="Markdown"
        )
        
    async def _admin_action_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        parts = query.data.split("_")
        action_type = parts[1]
        original_message = query.message.text
        if action_type == "confirm":
            op_type = parts[2]
            if op_type == "self":
                user_id = int(parts[3])
                amount = int(parts[4])
                await self._user_data_manager.add_user_balance(user_id, amount)
                await context.bot.send_message(
                    chat_id=user_id, text=f"✅ Ваш баланс успешно пополнен на {amount} ⭐️!"
                )
                await query.edit_message_text(
                    text=original_message + "\n\n**[ ✅ ПЛАТЁЖ ПОДТВЕРЖДЁН ]**",
                    parse_mode="Markdown",
                )
            elif op_type == "gift":
                payer_id, amount, recipient_username = int(parts[3]), int(parts[4]), parts[5]
                recipient_id = await self._user_data_manager.find_user_by_username(
                    recipient_username
                )
                if recipient_id:
                    await self._user_data_manager.add_user_balance(int(recipient_id), amount)
                    await context.bot.send_message(
                        chat_id=int(recipient_id),
                        text=f"🎁 Вам поступил подарок! Ваш баланс пополнен на {amount} ⭐️!",
                    )
                    await context.bot.send_message(
                        chat_id=payer_id,
                        text=f"✅ Ваш подарок на {amount} ⭐️ для @{recipient_username} успешно доставлен!",
                    )
                    await query.edit_message_text(
                        text=original_message
                        + f"\n\n**[ ✅ ПОДАРОК ДОСТАВЛЕН ]**\n(Получатель: @{recipient_username})",
                        parse_mode="Markdown",
                    )
                else:
                    await context.bot.send_message(
                        chat_id=payer_id,
                        text=f"❗️Ваш платёж подтверждён, но пользователь @{recipient_username} не найден. "
                        f"Звёзды не были зачислены. Свяжитесь с поддержкой @{self._support_username}.",
                    )
                    await query.edit_message_text(
                        text=original_message
                        + f"\n\n**[ ❌ ОШИБКА: ПОЛУЧАТЕЛЬ НЕ НАЙДЕН ]**\n(Пользователь: @{recipient_username})",
                        parse_mode="Markdown",
                    )
        elif action_type == "decline":
            user_id = int(parts[2])
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❗️ Ваш последний платёж был отклонён. Свяжитесь с поддержкой.\n @{self._support_username}",
            )
            await query.edit_message_text(
                text=original_message + "\n\n**[ ❌ ПЛАТЁЖ ОТКЛОНЁН ]**",
                parse_mode="Markdown",
            )

    async def _purpose_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        context.user_data.pop("friend_username", None)
        purpose = query.data
        if purpose == "purpose_self":
            context.user_data["awaiting_amount"] = True
            await query.edit_message_text("Введите сумму покупки (мин. 50):")
        elif purpose == "purpose_friend":
            context.user_data["awaiting_friend_username"] = True
            await query.edit_message_text("Введите @username друга:")

    async def _handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text.strip()
        if context.user_data.get("awaiting_friend_username"):
            await self._handle_friend_username(update, context, text)
            return
        if context.user_data.get("awaiting_amount"):
            await self._handle_amount_input(update, context, text)
            return
        if text == "⭐ Купить Звезды":
            await self._show_purchase_options(update)
        elif text == "👥 Реферальная система":
            await self._partner_program(update, context)
            
    async def _handle_friend_username(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        if not text.startswith("@"):
            await update.message.reply_text("Введите username, начиная с @. Пример: @username")
            return
        context.user_data["friend_username"] = text[1:]
        context.user_data.pop("awaiting_friend_username")
        context.user_data["awaiting_amount"] = True
        await update.message.reply_text(
            f"✅ Получатель: {text}\n\n"
            "Теперь введите сумму покупки (мин. 50).\n\n"
            "❗️**Важно:** Убедитесь, что вы правильно указали username. "
            "Если допустить ошибку, звёзды не будут зачислены.",
            parse_mode="Markdown",
        )

    async def _show_purchase_options(self, update: Update) -> None:
        keyboard = [
            [InlineKeyboardButton("Для себя", callback_data="purpose_self")],
            [InlineKeyboardButton("В подарок", callback_data="purpose_friend")],
        ]
        await update.message.reply_text(
            "🎁 Кому купить звёзды:", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def _partner_program(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        ref, earned = await self._user_data_manager.get_user_stats(user_id)
        # Теперь generate_referral_link использует имя из .env
        link = self._user_data_manager.generate_referral_link(user_id)
        text = (
            "<b>👥 Реферальная система</b>\n"
            "✨ Зарабатывайте 10% от расходов приглашённых!\n\n"
            f"<b>🔗 Ваша ссылка:</b>\n<code>{link}</code>\n\n"
            f"👥 Рефералов: <b>{ref}</b>\n"
            f"💸 Заработано: <b>{earned:.2f} RUB</b>"
        )
        keyboard = [
            [InlineKeyboardButton("📤 Поделиться", switch_inline_query=f"Покупайте звёзды: {link}")],
        ]
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )

    async def _set_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = [["⭐ Купить Звезды"], ["👥 Реферальная система"]]
        await update.message.reply_text(
            "Главное меню:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        
    def run(self) -> None:
        logger.info("✅ Бот запущен")
        self._application.run_polling()


if __name__ == "__main__":
    try:
        bot = TelegramBotApp()
        bot.run()
    except ValueError as e:
        logger.error(f"Ошибка инициализации: {e}")
