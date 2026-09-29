import os
import re
import uuid
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from html import escape

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# کمتر شدن لاگ‌های اضافی Telegram HTTP
logging.getLogger("httpx").setLevel(logging.WARNING)


# =========================================================
# ENV
# =========================================================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

CARD_NUMBER = os.getenv(
    "CARD_NUMBER",
    "شماره کارت در تنظیمات ربات وارد نشده است"
).strip()

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH",
    "telegram-webhook"
).strip("/")

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    ""
).strip()

PORT_RAW = os.getenv("PORT", "10000").strip()


if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN در Environment Variables تنظیم نشده است."
    )

if not ADMIN_ID_RAW:
    raise RuntimeError(
        "ADMIN_ID در Environment Variables تنظیم نشده است."
    )

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    raise RuntimeError(
        "ADMIN_ID باید یک عدد باشد."
    )

try:
    PORT = int(PORT_RAW)
except ValueError:
    raise RuntimeError(
        "PORT باید عدد باشد."
    )


if not WEBHOOK_URL:
    raise RuntimeError(
        "WEBHOOK_URL در Environment Variables تنظیم نشده است."
    )

if not WEBHOOK_PATH:
    raise RuntimeError(
        "WEBHOOK_PATH نمی‌تواند خالی باشد."
    )


# =========================================================
# STATES
# =========================================================

START = "start"
NAME = "name"
LAST_NAME = "last_name"
PHONE = "phone"
PRODUCT = "product"
PRODUCT_DETAILS = "product_details"
CART = "cart"
ADDRESS = "address"
SHIPPING = "shipping"
CUSTOMER_DESCRIPTION = "customer_description"
PRICE_STATUS = "price_status"
WAITING_ADMIN_PRICE = "waiting_admin_price"
PAYMENT = "payment"
RECEIPT = "receipt"
CONFIRM = "confirm"


# =========================================================
# CONSTANTS
# =========================================================

SHIPPING_POST = "پست"
SHIPPING_PICKUP = "دریافت توسط مشتری"


# =========================================================
# DATA MODELS
# =========================================================

@dataclass
class CartItem:
    product_type: str = "text"
    product_text: str = ""
    product_file_id: Optional[str] = None
    details: str = ""


@dataclass
class OrderData:
    order_id: str = field(
        default_factory=lambda: f"S-{uuid.uuid4().hex[:8].upper()}"
    )

    state: str = START

    first_name: str = ""
    last_name: str = ""
    phone: str = ""

    cart: List[CartItem] = field(default_factory=list)

    full_address: str = ""
    shipping_method: str = ""

    customer_description: str = ""

    price_known: bool = False
    price: str = ""

    receipt_file_id: Optional[str] = None
    receipt_type: str = ""

    waiting_for_admin_price: bool = False
    admin_price_message_id: Optional[int] = None


# =========================================================
# MEMORY STORAGE
# =========================================================

orders: Dict[int, OrderData] = {}

admin_price_requests: Dict[int, Dict] = {}


# =========================================================
# GLOBAL APPLICATION
# =========================================================

application: Optional[Application] = None


# =========================================================
# GENERAL HELPERS
# =========================================================

def normalize_text(value: str) -> str:
    return " ".join((value or "").strip().split())


def normalize_digits(value: str) -> str:
    value = str(value or "")

    translation = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )

    return value.translate(translation)


def normalize_price(value: str) -> Optional[str]:
    value = normalize_digits(value)

    # حذف جداکننده‌ها
    cleaned = re.sub(r"[,\s٬،]", "", value)

    # فقط عدد
    if not cleaned.isdigit():
        return None

    number = int(cleaned)

    if number <= 0:
        return None

    return f"{number:,}"


def get_message_text(update: Update) -> str:
    if not update.message:
        return ""

    return normalize_text(update.message.text or "")


def get_message_id(update: Update) -> Optional[int]:
    if not update.message:
        return None

    return update.message.message_id


def get_user_id(update: Update) -> Optional[int]:
    if not update.effective_user:
        return None

    return update.effective_user.id


def get_replied_message(update: Update):
    if not update.message:
        return None

    return update.message.reply_to_message


def get_replied_message_id(update: Update) -> Optional[int]:
    replied = get_replied_message(update)

    if not replied:
        return None

    return replied.message_id


def get_order(user_id: int) -> OrderData:
    if user_id not in orders:
        orders[user_id] = OrderData()

    return orders[user_id]


def reset_order(user_id: int) -> OrderData:
    cleanup_price_requests_for_user(user_id)

    orders[user_id] = OrderData()

    return orders[user_id]


def cleanup_price_request(message_id: Optional[int]):
    if message_id is None:
        return

    admin_price_requests.pop(message_id, None)


def cleanup_price_requests_for_user(user_id: int):
    to_delete = []

    for message_id, data in admin_price_requests.items():
        if data.get("user_id") == user_id:
            to_delete.append(message_id)

    for message_id in to_delete:
        admin_price_requests.pop(message_id, None)


def find_order_by_order_id(order_id: str):
    order_id = normalize_text(order_id).upper()

    for user_id, order in orders.items():
        if order.order_id.upper() == order_id:
            return user_id, order

    return None, None


def extract_order_id(text: str) -> Optional[str]:
    if not text:
        return None

    text = normalize_text(text).upper()

    patterns = [
        r"شماره\s*سفارش\s*[:：]?\s*([A-Z0-9-]+)",
        r"ORDER\s*ID\s*[:：]?\s*([A-Z0-9-]+)",
        r"(S-[A-Z0-9-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return match.group(1).upper()

    return None


def get_photo_file_id(update: Update) -> Optional[str]:
    if not update.message:
        return None

    if not update.message.photo:
        return None

    return update.message.photo[-1].file_id


def get_document_file_id(update: Update) -> Optional[str]:
    if not update.message:
        return None

    if not update.message.document:
        return None

    return update.message.document.file_id


async def context_bot_send_message(
    chat_id: int,
    text: str,
    **kwargs
):
    if application is None:
        raise RuntimeError(
            "Application هنوز ساخته نشده است."
        )

    return await application.bot.send_message(
        chat_id=chat_id,
        text=text,
        **kwargs
    )


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🛍 شروع خرید")
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def cancel_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ لغو سفارش",
                    callback_data="cancel_order"
                )
            ]
        ]
    )


def product_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ لغو سفارش",
                    callback_data="cancel_order"
                )
            ]
        ]
    )


def product_details_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏭ بدون توضیحات",
                    callback_data="skip_product_details"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو سفارش",
                    callback_data="cancel_order"
                )
            ]
        ]
    )


def cart_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ افزودن محصول دیگر",
                    callback_data="add_product"
                )
            ],
            [
                InlineKeyboardButton(
                    "➡️ ادامه سفارش",
                    callback_data="continue_order"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو سفارش",
                    callback_data="cancel_order"
                )
            ]
        ]
    )


def shipping_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📦 ارسال با پست",
                    callback_data="shipping_post"
                )
            ],
            [
                InlineKeyboardButton(
                    "🏪 دریافت توسط مشتری",
                    callback_data="shipping_pickup"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو سفارش",
                    callback_data="cancel_order"
                )
            ]
        ]
    )


def price_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💰 قیمت را می‌دانم",
                    callback_data="price_known"
                )
            ],
            [
                InlineKeyboardButton(
                    "❓ قیمت را نمی‌دانم",
                    callback_data="price_unknown"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو سفارش",
                    callback_data="cancel_order"
                )
            ]
        ]
    )


def receipt_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📤 ارسال رسید پرداخت",
                    callback_data="send_receipt"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو سفارش",
                    callback_data="cancel_order"
                )
            ]
        ]
    )


def confirm_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ تأیید نهایی سفارش",
                    callback_data="confirm_order"
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ ویرایش اطلاعات",
                    callback_data="edit_order"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو سفارش",
                    callback_data="cancel_order"
                )
            ]
        ]
    )


# =========================================================
# CART TEXT
# =========================================================

def build_cart_text(order: OrderData) -> str:
    if not order.cart:
        return "🛒 <b>سبد خرید خالی است.</b>"

    lines = [
        "🛒 <b>محصولات سفارش:</b>"
    ]

    for index, item in enumerate(order.cart, start=1):

        if item.product_type == "photo":
            product_title = "📷 محصول ارسال‌شده به‌صورت عکس"

        elif item.product_type == "document":
            product_title = "📄 محصول ارسال‌شده به‌صورت فایل"

        else:
            product_title = (
                escape(item.product_text)
                if item.product_text
                else "محصول متنی"
            )

        lines.append(
            f"\n<b>محصول {index}</b>\n"
            f"{product_title}"
        )

        if item.details:
            lines.append(
                f"\n📝 <b>توضیحات:</b> "
                f"{escape(item.details)}"
            )

    return "\n".join(lines)


# =========================================================
# CUSTOMER SUMMARY
# =========================================================

def build_customer_order_summary(order: OrderData) -> str:

    price_text = (
        f"{escape(order.price)} تومان"
        if order.price
        else "در انتظار تعیین قیمت"
    )

    return (
        "📋 <b>خلاصه سفارش شما</b>\n\n"

        f"🔖 <b>شماره سفارش:</b> "
        f"<code>{escape(order.order_id)}</code>\n\n"

        f"👤 <b>نام:</b> "
        f"{escape(order.first_name)} "
        f"{escape(order.last_name)}\n"

        f"📱 <b>شماره تماس:</b> "
        f"{escape(order.phone)}\n\n"

        f"{build_cart_text(order)}\n\n"

        f"📍 <b>آدرس:</b>\n"
        f"{escape(order.full_address)}\n\n"

        f"🚚 <b>روش دریافت:</b> "
        f"{escape(order.shipping_method)}\n\n"

        f"📝 <b>توضیحات مشتری:</b>\n"
        f"{escape(order.customer_description or 'ندارد')}\n\n"

        f"💰 <b>مبلغ:</b> "
        f"{price_text}\n\n"

        "لطفاً اطلاعات بالا را بررسی کنید و یکی از گزینه‌های زیر را انتخاب کنید."
    )


# =========================================================
# ADMIN SUMMARY
# =========================================================

def build_admin_order_summary(
    user_id: int,
    order: OrderData
) -> str:

    price_text = (
        f"{escape(order.price)} تومان"
        if order.price
        else "تعیین نشده"
    )

    return (
        "🛍 <b>سفارش جدید سورین</b>\n\n"

        f"🔖 <b>شماره سفارش:</b> "
        f"<code>{escape(order.order_id)}</code>\n"

        f"🆔 <b>Telegram ID:</b> "
        f"<code>{user_id}</code>\n\n"

        f"👤 <b>نام مشتری:</b> "
        f"{escape(order.first_name)} "
        f"{escape(order.last_name)}\n"

        f"📱 <b>شماره تماس:</b> "
        f"{escape(order.phone)}\n\n"

        f"{build_cart_text(order)}\n\n"

        f"📍 <b>آدرس:</b>\n"
        f"{escape(order.full_address)}\n\n"

        f"🚚 <b>روش دریافت:</b> "
        f"{escape(order.shipping_method)}\n\n"

        f"📝 <b>توضیحات مشتری:</b>\n"
        f"{escape(order.customer_description or 'ندارد')}\n\n"

        f"💰 <b>مبلغ:</b> "
        f"{price_text}\n"
    )


# =========================================================
# PROMPTS
# =========================================================

async def send_welcome(update: Update):

    text = (
        "سلام 👋\n\n"
        "به فروشگاه سورین خوش آمدید. 🛍\n\n"
        "برای شروع خرید روی دکمه زیر بزنید."
    )

    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=main_keyboard()
        )


async def ask_first_name(update: Update):

    if update.message:
        await update.message.reply_text(
            "👤 لطفاً <b>نام</b> خود را وارد کنید.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )


async def ask_last_name(update: Update):

    if update.message:
        await update.message.reply_text(
            "👤 حالا <b>نام خانوادگی</b> خود را وارد کنید.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )


async def ask_phone(update: Update):

    keyboard = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📱 ارسال شماره تماس",
                    request_contact=True
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    if update.message:
        await update.message.reply_text(
            "📱 لطفاً شماره تماس خود را ارسال کنید.\n\n"
            "می‌توانید از دکمه زیر برای ارسال مستقیم شماره استفاده کنید.",
            reply_markup=keyboard
        )


async def ask_product(update: Update):

    if update.message:
        await update.message.reply_text(
            "🛍 لطفاً محصول موردنظر را ارسال کنید.\n\n"
            "می‌توانید یکی از این موارد را بفرستید:\n"
            "📷 عکس محصول\n"
            "📄 فایل محصول\n"
            "📝 کد یا نام محصول",
            reply_markup=product_keyboard()
        )


async def ask_product_details(update: Update):

    if update.message:
        await update.message.reply_text(
            "📝 اگر برای این محصول توضیحی دارید، "
            "مثلاً رنگ، سایز یا تعداد، آن را بنویسید.\n\n"
            "اگر توضیحی ندارید، روی «بدون توضیحات» بزنید.",
            reply_markup=product_details_keyboard()
        )


async def ask_address(update: Update):

    if update.message:
        await update.message.reply_text(
            "📍 لطفاً <b>آدرس کامل</b> خود را وارد کنید.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )


async def ask_shipping(update: Update):

    if update.message:
        await update.message.reply_text(
            "🚚 روش دریافت سفارش را انتخاب کنید:",
            reply_markup=shipping_keyboard()
        )


async def ask_customer_description(update: Update):

    if update.message:
        await update.message.reply_text(
            "📝 اگر توضیح دیگری درباره سفارش دارید، "
            "اینجا بنویسید.\n\n"
            "اگر توضیحی ندارید، عبارت «ندارم» را ارسال کنید."
        )


async def ask_price_status(update: Update):

    if update.message:
        await update.message.reply_text(
            "💰 آیا قیمت محصول را می‌دانید؟",
            reply_markup=price_keyboard()
        )


async def ask_price(update: Update):

    if update.message:
        await update.message.reply_text(
            "💰 لطفاً مبلغ نهایی سفارش را به تومان وارد کنید.\n\n"
            "مثال:\n"
            "<code>850000</code>",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )


async def ask_receipt(update: Update):

    if update.message:
        await update.message.reply_text(
            "💳 مبلغ سفارش را به کارت زیر واریز کنید:\n\n"
            f"<code>{escape(CARD_NUMBER)}</code>\n\n"
            "سپس رسید پرداخت را به‌صورت عکس یا فایل ارسال کنید.",
            parse_mode="HTML",
            reply_markup=receipt_keyboard()
        )


# =========================================================
# START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = get_user_id(update)

    if user_id is None:
        return

    reset_order(user_id)

    await send_welcome(update)


# =========================================================
# SEND PRICE REQUEST TO ADMIN
# =========================================================

async def send_price_request_to_admin(
    user_id: int,
    order: OrderData
):

    text = (
        "💰 <b>درخواست قیمت جدید</b>\n\n"

        f"🔖 <b>شماره سفارش:</b> "
        f"<code>{escape(order.order_id)}</code>\n"

        f"👤 <b>مشتری:</b> "
        f"{escape(order.first_name)} "
        f"{escape(order.last_name)}\n"

        f"📱 <b>شماره:</b> "
        f"{escape(order.phone)}\n"

        f"🆔 <b>Telegram ID:</b> "
        f"<code>{user_id}</code>\n\n"

        f"{build_cart_text(order)}\n\n"

        "⬅️ برای تعیین قیمت، روی <b>همین پیام Reply</b> کنید "
        "و مبلغ را بفرستید.\n\n"

        "مثال:\n"
        "<code>850000</code>"
    )

    admin_message = await context_bot_send_message(
        ADMIN_ID,
        text,
        parse_mode="HTML"
    )

    admin_message_id = admin_message.message_id

    order.admin_price_message_id = admin_message_id
    order.waiting_for_admin_price = True
    order.state = WAITING_ADMIN_PRICE

    admin_price_requests[admin_message_id] = {
        "user_id": user_id,
        "order_id": order.order_id,
    }


# =========================================================
# DELIVER ADMIN PRICE
# =========================================================

async def deliver_admin_price(
    user_id: int,
    order: OrderData,
    price: str,
    admin_message_id: Optional[int] = None
):

    try:

        await context_bot_send_message(
            user_id,
            (
                "💰 <b>قیمت سفارش شما مشخص شد.</b>\n\n"
                f"🔖 شماره سفارش: "
                f"<code>{escape(order.order_id)}</code>\n\n"
                f"💵 مبلغ قابل پرداخت: "
                f"<b>{escape(price)} تومان</b>\n\n"
                "پس از پرداخت، رسید را ارسال کنید."
            ),
            parse_mode="HTML",
            reply_markup=receipt_keyboard()
        )

    except TelegramError as exc:

        logger.exception(
            "Could not send admin price to customer %s",
            user_id
        )

        try:
            await context_bot_send_message(
                ADMIN_ID,
                (
                    "❌ ارسال قیمت به مشتری انجام نشد.\n\n"
                    f"شماره سفارش: "
                    f"<code>{escape(order.order_id)}</code>\n"
                    f"خطا: <code>{escape(str(exc))}</code>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            logger.exception(
                "Could not notify admin about failed price delivery."
            )

        return False

    order.price = price
    order.price_known = True
    order.waiting_for_admin_price = False
    order.state = RECEIPT

    cleanup_price_request(admin_message_id)

    if admin_message_id != order.admin_price_message_id:
        cleanup_price_request(order.admin_price_message_id)

    order.admin_price_message_id = None

    try:
        await context_bot_send_message(
            ADMIN_ID,
            (
                "✅ قیمت با موفقیت برای مشتری ارسال شد.\n\n"
                f"🔖 شماره سفارش: "
                f"<code>{escape(order.order_id)}</code>\n"
                f"💰 مبلغ: <b>{escape(price)} تومان</b>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        logger.exception(
            "Could not notify admin about successful price delivery."
        )

    return True


# =========================================================
# SEND FINAL ORDER TO ADMIN
# =========================================================

async def send_final_order_to_admin(
    user_id: int,
    order: OrderData
):

    try:

        summary = build_admin_order_summary(
            user_id,
            order
        )

        await context_bot_send_message(
            ADMIN_ID,
            summary,
            parse_mode="HTML"
        )

    except Exception as exc:

        logger.exception(
            "Could not send admin order summary."
        )

        try:
            await context_bot_send_message(
                ADMIN_ID,
                (
                    "❌ خطا در ارسال خلاصه سفارش\n\n"
                    f"شماره سفارش: "
                    f"<code>{escape(order.order_id)}</code>\n"
                    f"خطا: <code>{escape(str(exc))}</code>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            logger.exception(
                "Could not send summary error to admin."
            )

        return False

    # -----------------------------------------------------
    # SEND PRODUCT MEDIA
    # -----------------------------------------------------

    for index, item in enumerate(order.cart, start=1):

        try:

            caption = (
                f"🛍 <b>محصول {index}</b>\n"
                f"🔖 سفارش: "
                f"<code>{escape(order.order_id)}</code>"
            )

            if item.details:
                caption += (
                    f"\n📝 توضیحات: "
                    f"{escape(item.details)}"
                )

            if item.product_type == "photo":

                await application.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=item.product_file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif item.product_type == "document":

                await application.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=item.product_file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

        except Exception:

            logger.exception(
                "Could not send product media %s",
                index
            )

    # -----------------------------------------------------
    # SEND RECEIPT
    # -----------------------------------------------------

    if order.receipt_file_id:

        try:

            receipt_caption = (
                "💳 <b>رسید پرداخت سفارش</b>\n\n"
                f"🔖 شماره سفارش: "
                f"<code>{escape(order.order_id)}</code>\n"
                f"👤 مشتری: "
                f"{escape(order.first_name)} "
                f"{escape(order.last_name)}\n"
                f"🆔 Telegram ID: "
                f"<code>{user_id}</code>"
            )

            if order.receipt_type == "photo":

                await application.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=order.receipt_file_id,
                    caption=receipt_caption,
                    parse_mode="HTML"
                )

            elif order.receipt_type == "document":

                await application.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=order.receipt_file_id,
                    caption=receipt_caption,
                    parse_mode="HTML"
                )

        except Exception as exc:

            logger.exception(
                "Could not send payment receipt."
            )

            try:
                await context_bot_send_message(
                    ADMIN_ID,
                    (
                        "❌ ارسال رسید پرداخت انجام نشد.\n\n"
                        f"شماره سفارش: "
                        f"<code>{escape(order.order_id)}</code>\n"
                        f"خطا: <code>{escape(str(exc))}</code>"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                logger.exception(
                    "Could not notify admin about receipt failure."
                )

            return False

    # -----------------------------------------------------
    # FINAL ADMIN MESSAGE
    # -----------------------------------------------------

    try:

        await context_bot_send_message(
            ADMIN_ID,
            (
                "✅ <b>سفارش کامل دریافت شد.</b>\n\n"
                f"🔖 شماره سفارش: "
                f"<code>{escape(order.order_id)}</code>\n"
                "📦 اطلاعات سفارش، محصولات و رسید ارسال شدند."
            ),
            parse_mode="HTML"
        )

    except Exception:
        logger.exception(
            "Could not send final admin notification."
        )

    return True


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user_id = query.from_user.id
    order = get_order(user_id)

    data = query.data or ""

    # -----------------------------------------------------
    # CANCEL
    # -----------------------------------------------------

    if data == "cancel_order":

        reset_order(user_id)

        await query.edit_message_text(
            "❌ سفارش لغو شد.\n\n"
            "هر زمان خواستید می‌توانید دوباره خرید را شروع کنید."
        )

        if query.message:
            await query.message.reply_text(
                "🛍 برای شروع دوباره:",
                reply_markup=main_keyboard()
            )

        return

    # -----------------------------------------------------
    # START SHOPPING
    # -----------------------------------------------------

    if data == "start_shopping":

        reset_order(user_id)

        order = get_order(user_id)

        order.state = NAME

        await query.edit_message_text(
            "🛍 خرید جدید شروع شد."
        )

        await ask_first_name(update)

        return

    # -----------------------------------------------------
    # ADD PRODUCT
    # -----------------------------------------------------

    if data == "add_product":

        order.state = PRODUCT

        await query.edit_message_text(
            "➕ افزودن محصول جدید"
        )

        await ask_product(update)

        return

    # -----------------------------------------------------
    # CONTINUE ORDER
    # -----------------------------------------------------

    if data == "continue_order":

        if not order.cart:

            await query.edit_message_text(
                "⚠️ ابتدا حداقل یک محصول اضافه کنید."
            )

            return

        order.state = ADDRESS

        await query.edit_message_text(
            "➡️ ادامه سفارش"
        )

        await ask_address(update)

        return

    # -----------------------------------------------------
    # SKIP PRODUCT DETAILS
    # -----------------------------------------------------

    if data == "skip_product_details":

        order.state = CART

        if query.message:

            await query.edit_message_text(
                "✅ توضیحات این محصول ثبت نشد."
            )

            await query.message.reply_text(
                build_cart_text(order),
                parse_mode="HTML",
                reply_markup=cart_keyboard()
            )

        return

    # -----------------------------------------------------
    # SHIPPING POST
    # -----------------------------------------------------

    if data == "shipping_post":

        order.shipping_method = SHIPPING_POST
        order.state = CUSTOMER_DESCRIPTION

        await query.edit_message_text(
            "📦 روش ارسال: پست"
        )

        await ask_customer_description(update)

        return

    # -----------------------------------------------------
    # SHIPPING PICKUP
    # -----------------------------------------------------

    if data == "shipping_pickup":

        order.shipping_method = SHIPPING_PICKUP
        order.state = CUSTOMER_DESCRIPTION

        await query.edit_message_text(
            "🏪 روش دریافت: دریافت توسط مشتری"
        )

        await ask_customer_description(update)

        return

    # -----------------------------------------------------
    # PRICE KNOWN
    # -----------------------------------------------------

    if data == "price_known":

        order.price_known = True
        order.state = PAYMENT

        await query.edit_message_text(
            "💰 قیمت مشخص است."
        )

        await ask_price(update)

        return

    # -----------------------------------------------------
    # PRICE UNKNOWN
    # -----------------------------------------------------

    if data == "price_unknown":

        order.price_known = False

        await query.edit_message_text(
            "⏳ درخواست قیمت برای مدیریت فروشگاه ارسال می‌شود..."
        )

        try:

            await send_price_request_to_admin(
                user_id,
                order
            )

            await context_bot_send_message(
                user_id,
                (
                    "⏳ <b>درخواست قیمت ارسال شد.</b>\n\n"
                    f"🔖 شماره سفارش: "
                    f"<code>{escape(order.order_id)}</code>\n\n"
                    "پس از تعیین قیمت، مبلغ برای شما ارسال می‌شود."
                ),
                parse_mode="HTML"
            )

        except Exception as exc:

            logger.exception(
                "Could not send price request."
            )

            order.waiting_for_admin_price = False
            order.state = PRICE_STATUS

            await context_bot_send_message(
                user_id,
                (
                    "❌ ارسال درخواست قیمت با مشکل مواجه شد.\n\n"
                    f"خطا: <code>{escape(str(exc))}</code>\n\n"
                    "لطفاً دوباره تلاش کنید."
                ),
                parse_mode="HTML",
                reply_markup=price_keyboard()
            )

        return

    # -----------------------------------------------------
    # SEND RECEIPT
    # -----------------------------------------------------

    if data == "send_receipt":

        order.state = RECEIPT

        await query.edit_message_text(
            "📤 لطفاً رسید پرداخت را به‌صورت عکس یا فایل ارسال کنید."
        )

        return

    # -----------------------------------------------------
    # CONFIRM ORDER
    # -----------------------------------------------------

    if data == "confirm_order":

        if not order.receipt_file_id:

            await query.answer(
                "ابتدا رسید پرداخت را ارسال کنید.",
                show_alert=True
            )

            return

        success = await send_final_order_to_admin(
            user_id,
            order
        )

        if not success:

            await query.answer(
                "ارسال سفارش به مدیریت با مشکل مواجه شد.",
                show_alert=True
            )

            return

        order.state = START

        await query.edit_message_text(
            "✅ <b>سفارش شما با موفقیت ثبت شد.</b>\n\n"
            f"🔖 شماره سفارش: "
            f"<code>{escape(order.order_id)}</code>\n\n"
            "سفارش برای مدیریت فروشگاه ارسال شد.\n"
            "از خرید شما متشکریم ❤️",
            parse_mode="HTML"
        )

        return

    # -----------------------------------------------------
    # EDIT ORDER
    # -----------------------------------------------------

    if data == "edit_order":

        reset_order(user_id)

        order = get_order(user_id)

        order.state = NAME

        await query.edit_message_text(
            "✏️ ویرایش سفارش شروع شد."
        )

        await ask_first_name(update)

        return


# =========================================================
# ADMIN MESSAGE HANDLER
# =========================================================

async def handle_admin_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    # فقط مدیر
    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    text = normalize_text(update.message.text or "")

    # -----------------------------------------------------
    # LEGACY:
    # /price USER_ID PRICE
    # -----------------------------------------------------

    if text.lower().startswith("/price"):

        parts = text.split()

        if len(parts) != 3:

            await update.message.reply_text(
                "فرمت صحیح:\n\n"
                "/price USER_ID PRICE\n\n"
                "مثال:\n"
                "/price 123456789 850000"
            )

            return

        try:
            customer_id = int(parts[1])
        except ValueError:

            await update.message.reply_text(
                "❌ USER_ID باید عدد باشد."
            )

            return

        price = normalize_price(parts[2])

        if price is None:

            await update.message.reply_text(
                "❌ مبلغ نامعتبر است."
            )

            return

        order = orders.get(customer_id)

        if not order:

            await update.message.reply_text(
                "❌ سفارشی برای این کاربر پیدا نشد."
            )

            return

        if not order.waiting_for_admin_price:

            await update.message.reply_text(
                "⚠️ این سفارش در انتظار قیمت نیست."
            )

            return

        await deliver_admin_price(
            customer_id,
            order,
            price,
            order.admin_price_message_id
        )

        return

    # -----------------------------------------------------
    # REPLY TO PRICE REQUEST
    # -----------------------------------------------------

    replied_message = update.message.reply_to_message

    if replied_message:

        replied_message_id = replied_message.message_id

        request_data = admin_price_requests.get(
            replied_message_id
        )

        user_id = None
        order = None

        # مسیر اصلی: ID پیام ذخیره شده
        if request_data:

            user_id = request_data.get("user_id")

            order_id = request_data.get("order_id")

            if user_id is not None:
                candidate_order = orders.get(user_id)

                if (
                    candidate_order
                    and candidate_order.order_id == order_id
                ):
                    order = candidate_order

        # مسیر پشتیبان: استخراج Order ID از متن
        if order is None:

            replied_text = (
                replied_message.text
                or replied_message.caption
                or ""
            )

            order_id = extract_order_id(
                replied_text
            )

            if order_id:

                user_id, order = find_order_by_order_id(
                    order_id
                )

        if order is None or user_id is None:

            await update.message.reply_text(
                "❌ نتوانستم سفارش مربوط به این Reply را پیدا کنم.\n\n"
                "مطمئن شوید که مستقیماً روی پیام «درخواست قیمت» Reply کرده‌اید."
            )

            return

        if not order.waiting_for_admin_price:

            await update.message.reply_text(
                "⚠️ این سفارش دیگر در انتظار قیمت نیست."
            )

            return

        price = normalize_price(text)

        if price is None:

            await update.message.reply_text(
                "❌ مبلغ معتبر نیست.\n\n"
                "فقط مبلغ را ارسال کنید.\n"
                "مثال:\n"
                "850000"
            )

            return

        success = await deliver_admin_price(
            user_id,
            order,
            price,
            replied_message_id
        )

        if not success:

            await update.message.reply_text(
                "❌ ارسال قیمت به مشتری انجام نشد."
            )

        return

    # -----------------------------------------------------
    # NORMAL ADMIN MESSAGE
    # -----------------------------------------------------

    await update.message.reply_text(
        "برای تعیین قیمت سفارش، روی پیام «درخواست قیمت» Reply کنید "
        "و فقط مبلغ را ارسال کنید.\n\n"
        "مثال:\n"
        "850000"
    )


# =========================================================
# CUSTOMER MESSAGE HANDLER
# =========================================================

async def customer_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    user_id = get_user_id(update)

    if user_id is None:
        return

    # -----------------------------------------------------
    # ADMIN
    # -----------------------------------------------------

    if user_id == ADMIN_ID:

        await handle_admin_message(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # ORDER
    # -----------------------------------------------------

    order = get_order(user_id)

    state = order.state

    text = get_message_text(update)

    # -----------------------------------------------------
    # START
    # -----------------------------------------------------

    if state == START:

        if text == "🛍 شروع خرید":

            reset_order(user_id)

            order = get_order(user_id)

            order.state = NAME

            await ask_first_name(update)

            return

        await update.message.reply_text(
            "برای شروع خرید روی دکمه زیر بزنید.",
            reply_markup=main_keyboard()
        )

        return

    # -----------------------------------------------------
    # NAME
    # -----------------------------------------------------

    if state == NAME:

        if not text:

            await update.message.reply_text(
                "❌ لطفاً نام خود را وارد کنید."
            )

            return

        order.first_name = text
        order.state = LAST_NAME

        await ask_last_name(update)

        return

    # -----------------------------------------------------
    # LAST NAME
    # -----------------------------------------------------

    if state == LAST_NAME:

        if not text:

            await update.message.reply_text(
                "❌ لطفاً نام خانوادگی خود را وارد کنید."
            )

            return

        order.last_name = text
        order.state = PHONE

        await ask_phone(update)

        return

    # -----------------------------------------------------
    # PHONE
    # -----------------------------------------------------

    if state == PHONE:

        phone = ""

        if update.message.contact:

            phone = normalize_digits(
                update.message.contact.phone_number
            ).strip()

        elif text:

            phone = normalize_digits(
                text
            ).strip()

        # حذف فاصله و خط تیره
        phone = re.sub(
            r"[\s-]",
            "",
            phone
        )

        if not re.fullmatch(
            r"\+?\d{10,15}",
            phone
        ):

            await update.message.reply_text(
                "❌ شماره تلفن معتبر نیست.\n\n"
                "مثال:\n"
                "+989121234567\n\n"
                "یا از دکمه ارسال شماره استفاده کنید."
            )

            return

        order.phone = phone

        await update.message.reply_text(
            "✅ شماره تماس ثبت شد.",
            reply_markup=ReplyKeyboardRemove()
        )

        order.state = PRODUCT

        await ask_product(update)

        return

    # -----------------------------------------------------
    # PRODUCT
    # -----------------------------------------------------

    if state == PRODUCT:

        photo_file_id = get_photo_file_id(update)

        document_file_id = get_document_file_id(update)

        if photo_file_id:

            item = CartItem(
                product_type="photo",
                product_file_id=photo_file_id
            )

        elif document_file_id:

            item = CartItem(
                product_type="document",
                product_file_id=document_file_id
            )

        elif text:

            item = CartItem(
                product_type="text",
                product_text=text
            )

        else:

            await update.message.reply_text(
                "❌ لطفاً عکس، فایل یا کد/نام محصول را ارسال کنید."
            )

            return

        order.cart.append(item)

        order.state = PRODUCT_DETAILS

        await update.message.reply_text(
            "✅ محصول اضافه شد."
        )

        await ask_product_details(update)

        return

    # -----------------------------------------------------
    # PRODUCT DETAILS
    # -----------------------------------------------------

    if state == PRODUCT_DETAILS:

        if text:

            order.cart[-1].details = text

        order.state = CART

        await update.message.reply_text(
            "✅ اطلاعات محصول ثبت شد.\n\n"
            + build_cart_text(order),
            parse_mode="HTML",
            reply_markup=cart_keyboard()
        )

        return

    # -----------------------------------------------------
    # CART
    # -----------------------------------------------------

    if state == CART:

        await update.message.reply_text(
            "لطفاً یکی از گزینه‌های زیر را انتخاب کنید.",
            reply_markup=cart_keyboard()
        )

        return

    # -----------------------------------------------------
    # ADDRESS
    # -----------------------------------------------------

    if state == ADDRESS:

        if len(text) < 10:

            await update.message.reply_text(
                "❌ لطفاً آدرس کامل‌تری وارد کنید."
            )

            return

        order.full_address = text
        order.state = SHIPPING

        await ask_shipping(update)

        return

    # -----------------------------------------------------
    # CUSTOMER DESCRIPTION
    # -----------------------------------------------------

    if state == CUSTOMER_DESCRIPTION:

        if text.lower() in [
            "ندارم",
            "ندارد",
            "خیر",
            "نه"
        ]:

            order.customer_description = ""

        else:

            order.customer_description = text

        order.state = PRICE_STATUS

        await ask_price_status(update)

        return

    # -----------------------------------------------------
    # PRICE STATUS
    # -----------------------------------------------------

    if state == PRICE_STATUS:

        await update.message.reply_text(
            "لطفاً یکی از گزینه‌های مربوط به قیمت را انتخاب کنید.",
            reply_markup=price_keyboard()
        )

        return

    # -----------------------------------------------------
    # PAYMENT / ENTER PRICE
    # -----------------------------------------------------

    if state == PAYMENT:

        price = normalize_price(text)

        if price is None:

            await update.message.reply_text(
                "❌ مبلغ نامعتبر است.\n\n"
                "مثال:\n"
                "850000"
            )

            return

        order.price = price
        order.price_known = True
        order.state = RECEIPT

        await update.message.reply_text(
            f"✅ مبلغ {escape(price)} تومان ثبت شد.",
            parse_mode="HTML"
        )

        await ask_receipt(update)

        return

    # -----------------------------------------------------
    # WAITING ADMIN PRICE
    # -----------------------------------------------------

    if state == WAITING_ADMIN_PRICE:

        await update.message.reply_text(
            "⏳ هنوز قیمت سفارش توسط مدیریت مشخص نشده است.\n\n"
            "لطفاً منتظر بمانید."
        )

        return

    # -----------------------------------------------------
    # RECEIPT
    # -----------------------------------------------------

    if state == RECEIPT:

        photo_file_id = get_photo_file_id(update)

        document_file_id = get_document_file_id(update)

        if photo_file_id:

            order.receipt_file_id = photo_file_id
            order.receipt_type = "photo"

        elif document_file_id:

            order.receipt_file_id = document_file_id
            order.receipt_type = "document"

        else:

            await update.message.reply_text(
                "❌ لطفاً رسید را به‌صورت عکس یا فایل ارسال کنید."
            )

            return

        order.state = CONFIRM

        await update.message.reply_text(
            "✅ رسید پرداخت دریافت شد.\n\n"
            + build_customer_order_summary(order),
            parse_mode="HTML",
            reply_markup=confirm_keyboard()
        )

        return

    # -----------------------------------------------------
    # CONFIRM
    # -----------------------------------------------------

    if state == CONFIRM:

        await update.message.reply_text(
            "لطفاً از دکمه‌های زیر برای تأیید یا ویرایش سفارش استفاده کنید.",
            reply_markup=confirm_keyboard()
        )

        return


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "Unhandled exception:",
        exc_info=context.error
    )


# =========================================================
# BUILD APPLICATION
# =========================================================

def build_application() -> Application:

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            customer_message_handler
        )
    )

    app.add_error_handler(
        error_handler
    )

    return app


# =========================================================
# MAIN
# =========================================================

def main():

    global application

    application = build_application()

    webhook_url = (
        f"{WEBHOOK_URL}/{WEBHOOK_PATH}"
    )

    logger.info(
        "========================================"
    )

    logger.info(
        "Suryan Telegram Bot is starting..."
    )

    logger.info(
        "Admin ID: %s",
        ADMIN_ID
    )

    logger.info(
        "Webhook URL: %s",
        webhook_url
    )

    logger.info(
        "Port: %s",
        PORT
    )

    logger.info(
        "========================================"
    )

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
