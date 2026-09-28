import os
import re
import uuid
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict

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
# CONFIG
# =========================================================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CARD_NUMBER = os.getenv(
    "CARD_NUMBER",
    "شماره کارت در تنظیمات ربات وارد نشده است"
)

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN در فایل .env تنظیم نشده است"
    )

if not ADMIN_ID_RAW:
    raise RuntimeError(
        "ADMIN_ID در فایل .env تنظیم نشده است"
    )

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    raise RuntimeError(
        "ADMIN_ID باید عددی باشد"
    )


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# CONSTANTS
# =========================================================

SHIPPING_POST = "پست"
SHIPPING_PICKUP = "دریافت توسط مشتری"


# =========================================================
# STATES
# =========================================================

START = "start"
NAME = "name"
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

    # Telegram message ID of the admin price-request message
    admin_price_message_id: Optional[int] = None


# =========================================================
# STORAGE
# =========================================================

orders: Dict[int, OrderData] = {}

# admin_message_id -> {
#     "user_id": int,
#     "order_id": str
# }
admin_price_requests: Dict[int, Dict] = {}


# =========================================================
# TEXT HELPERS
# =========================================================

def normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""

    text = text.strip()

    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ۀ": "ه",
        "ة": "ه",
        "\u200c": " ",
        "\u200f": "",
        "\u200e": "",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def normalize_digits(text: Optional[str]) -> str:
    if not text:
        return ""

    replacements = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )

    return text.translate(replacements)


def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None

    text = normalize_digits(text)
    text = text.replace(",", "")
    text = text.replace("٬", "")
    text = text.replace("تومان", "")
    text = text.replace("تومن", "")
    text = text.replace("ریال", "")
    text = text.replace(" ", "")
    text = text.strip()

    if not text.isdigit():
        return None

    return text


def get_message_text(message) -> str:
    if not message:
        return ""

    return normalize_text(
        getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    )


def get_message_id(message) -> Optional[int]:
    if not message:
        return None

    return getattr(message, "message_id", None)


def get_user_id(update: Update) -> Optional[int]:
    if update.effective_user:
        return update.effective_user.id

    return None


# =========================================================
# REPLY MESSAGE HELPERS
# =========================================================

def get_replied_message(message):
    if not message:
        return None

    return getattr(message, "reply_to_message", None)


def get_replied_message_id(message) -> Optional[int]:
    replied = get_replied_message(message)

    if not replied:
        return None

    return getattr(replied, "message_id", None)


# =========================================================
# ORDER HELPERS
# =========================================================

def get_order(user_id: int) -> OrderData:
    if user_id not in orders:
        orders[user_id] = OrderData()

    return orders[user_id]


def cleanup_price_request(order: OrderData):
    if order.admin_price_message_id is not None:
        admin_price_requests.pop(
            order.admin_price_message_id,
            None
        )

    order.admin_price_message_id = None


def reset_order(user_id: int):
    old_order = orders.get(user_id)

    if old_order:
        cleanup_price_request(old_order)

    orders[user_id] = OrderData()


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

    # مثال:
    # شماره سفارش: S-ABC12345
    # شماره سفارش *: S-ABC12345

    patterns = [
        r"شماره\s*سفارش\s*[:：]?\s*([A-Z0-9-]+)",
        r"ORDER\s*ID\s*[:：]?\s*([A-Z0-9-]+)",
        r"(S-[A-Z0-9-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            return match.group(1)

    return None


# =========================================================
# MEDIA HELPERS
# =========================================================

def get_photo_file_id(message) -> Optional[str]:
    if not message:
        return None

    photos = getattr(message, "photo", None)

    if not photos:
        return None

    try:
        # آخرین PhotoSize معمولاً باکیفیت‌ترین است
        photo = photos[-1]

        return getattr(photo, "file_id", None)

    except Exception:
        return None


def get_document_file_id(message) -> Optional[str]:
    if not message:
        return None

    document = getattr(message, "document", None)

    if not document:
        return None

    return getattr(document, "file_id", None)


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🛍 شروع خرید",
                callback_data="start_shopping"
            )
        ]
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order"
            )
        ]
    ])


def product_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order"
            )
        ]
    ])


def cart_keyboard():
    return InlineKeyboardMarkup([
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
        ],
    ])


def product_details_keyboard():
    return InlineKeyboardMarkup([
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
        ],
    ])


def shipping_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📦 پست",
                callback_data="shipping_post"
            ),
            InlineKeyboardButton(
                "🏪 دریافت توسط مشتری",
                callback_data="shipping_pickup"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order"
            )
        ],
    ])


def price_keyboard():
    return InlineKeyboardMarkup([
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
        ],
    ])


def receipt_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📸 ارسال رسید پرداخت",
                callback_data="send_receipt"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order"
            )
        ],
    ])


def confirm_keyboard():
    return InlineKeyboardMarkup([
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
        ],
    ])


# =========================================================
# ORDER TEXT
# =========================================================

def build_cart_text(order: OrderData) -> str:
    if not order.cart:
        return "سبد خرید خالی است."

    lines = ["🛒 محصولات سفارش:"]

    for index, item in enumerate(order.cart, start=1):

        if item.product_type == "photo":
            product_name = "📷 محصول ارسال‌شده به صورت عکس"

        elif item.product_type == "document":
            product_name = "📄 محصول ارسال‌شده به صورت فایل"

        else:
            product_name = item.product_text or "محصول بدون نام"

        lines.append(
            f"\n{index}. {product_name}"
        )

        if item.details:
            lines.append(
                f"   📝 جزئیات: {item.details}"
            )

    return "\n".join(lines)


def build_customer_order_summary(order: OrderData) -> str:
    price_text = (
        f"{order.price} تومان"
        if order.price
        else "نامشخص"
    )

    receipt_text = (
        "✅ رسید پرداخت دریافت شده"
        if order.receipt_file_id
        else "❌ رسید دریافت نشده"
    )

    return (
        "🧾 <b>اطلاعات کامل سفارش</b>\n"
        "\n"
        f"🔖 <b>شماره سفارش:</b> <code>{order.order_id}</code>\n"
        "\n"
        f"👤 <b>نام:</b> "
        f"{order.first_name} {order.last_name}\n"
        f"📱 <b>شماره موبایل:</b> {order.phone}\n"
        "\n"
        f"{build_cart_text(order)}\n"
        "\n"
        f"📍 <b>آدرس:</b>\n{order.full_address}\n"
        "\n"
        f"🚚 <b>روش ارسال:</b> {order.shipping_method}\n"
        "\n"
        f"📝 <b>توضیحات سفارش:</b>\n"
        f"{order.customer_description or 'ندارد'}\n"
        "\n"
        f"💰 <b>مبلغ:</b> {price_text}\n"
        "\n"
        f"💳 <b>وضعیت رسید:</b> {receipt_text}\n"
        "\n"
        "⚠️ لطفاً اطلاعات بالا را با دقت بررسی کنید.\n"
        "در صورت صحیح بودن اطلاعات، روی «تأیید نهایی سفارش» بزنید."
    )


def build_admin_order_summary(
    order: OrderData,
    customer_id: int
) -> str:

    price_text = (
        f"{order.price} تومان"
        if order.price
        else "نامشخص"
    )

    return (
        "🛍 <b>سفارش جدید</b>\n"
        "\n"
        f"🔖 <b>شماره سفارش:</b> "
        f"<code>{order.order_id}</code>\n"
        f"👤 <b>Telegram ID:</b> "
        f"<code>{customer_id}</code>\n"
        "\n"
        f"👤 <b>نام:</b> "
        f"{order.first_name} {order.last_name}\n"
        f"📱 <b>شماره:</b> {order.phone}\n"
        "\n"
        f"{build_cart_text(order)}\n"
        "\n"
        f"📍 <b>آدرس:</b>\n{order.full_address}\n"
        "\n"
        f"🚚 <b>روش ارسال:</b> {order.shipping_method}\n"
        "\n"
        f"📝 <b>توضیحات مشتری:</b>\n"
        f"{order.customer_description or 'ندارد'}\n"
        "\n"
        f"💰 <b>مبلغ:</b> {price_text}\n"
        "\n"
        "💳 <b>رسید پرداخت:</b> ارسال شده"
    )


# =========================================================
# CUSTOMER MESSAGES
# =========================================================

async def send_welcome(update: Update):
    await update.message.reply_text(
        "سلام 👋\n"
        "به ربات فروشگاه سورین خوش آمدید.\n\n"
        "برای شروع خرید روی دکمه زیر بزنید.",
        reply_markup=main_keyboard(),
    )


async def ask_first_name(update: Update):
    await update.message.reply_text(
        "👤 لطفاً نام خود را وارد کنید:",
        reply_markup=cancel_keyboard(),
    )


async def ask_last_name(update: Update):
    await update.message.reply_text(
        "👤 لطفاً نام خانوادگی خود را وارد کنید:",
        reply_markup=cancel_keyboard(),
    )


async def ask_phone(update: Update):
    keyboard = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📱 ارسال شماره موبایل",
                    request_contact=True,
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(
        "📱 لطفاً شماره موبایل خود را ارسال کنید.\n"
        "می‌توانید از دکمه زیر برای ارسال خودکار شماره استفاده کنید.",
        reply_markup=keyboard,
    )


async def ask_product(update: Update):
    await update.message.reply_text(
        "🛍 لطفاً محصول موردنظر را ارسال کنید.\n\n"
        "می‌توانید یکی از موارد زیر را بفرستید:\n"
        "• عکس محصول\n"
        "• فایل محصول\n"
        "• کد یا نام محصول",
        reply_markup=product_keyboard(),
    )


async def ask_product_details(update: Update):
    await update.message.reply_text(
        "📝 اگر توضیحی درباره این محصول دارید، همینجا ارسال کنید.\n\n"
        "مثلاً:\n"
        "• سایز L\n"
        "• رنگ مشکی\n"
        "• تعداد ۲ عدد\n\n"
        "اگر توضیحی ندارید، روی دکمه زیر بزنید.",
        reply_markup=product_details_keyboard(),
    )


async def ask_address(update: Update):
    await update.message.reply_text(
        "📍 لطفاً آدرس کامل خود را در یک پیام ارسال کنید.\n\n"
        "مثال:\n"
        "استان، شهر، خیابان، کوچه، پلاک، واحد، کد پستی",
        reply_markup=cancel_keyboard(),
    )


async def ask_shipping(update: Update):
    await update.message.reply_text(
        "🚚 لطفاً روش دریافت سفارش را انتخاب کنید:",
        reply_markup=shipping_keyboard(),
    )


async def ask_customer_description(update: Update):
    await update.message.reply_text(
        "📝 اگر توضیح دیگری درباره سفارش دارید ارسال کنید.\n\n"
        "اگر توضیحی ندارید، روی «بدون توضیحات» بزنید.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⏭ بدون توضیحات",
                    callback_data="skip_customer_description",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو سفارش",
                    callback_data="cancel_order",
                )
            ],
        ]),
    )


async def ask_price_status(update: Update):
    await update.message.reply_text(
        "💰 وضعیت قیمت محصول را مشخص کنید:",
        reply_markup=price_keyboard(),
    )


async def ask_price(update: Update):
    await update.message.reply_text(
        "💰 لطفاً مبلغ سفارش را فقط به صورت عدد وارد کنید.\n\n"
        "مثال:\n"
        "850000",
        reply_markup=cancel_keyboard(),
    )


async def ask_receipt(update: Update):
    await update.message.reply_text(
        "💳 لطفاً مبلغ را به کارت زیر واریز کنید:\n\n"
        f"<code>{CARD_NUMBER}</code>\n\n"
        "سپس عکس یا فایل رسید پرداخت را ارسال کنید.",
        reply_markup=receipt_keyboard(),
        parse_mode="HTML",
    )


# =========================================================
# START COMMAND
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
# CUSTOMER CALLBACKS
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
    data = query.data or ""

    order = get_order(user_id)

    # -----------------------------------------------------
    # CANCEL
    # -----------------------------------------------------

    if data == "cancel_order":

        reset_order(user_id)

        await query.message.edit_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "❌ سفارش لغو شد.\n\n"
            "هر زمان خواستید می‌توانید دوباره خرید را شروع کنید.",
            reply_markup=main_keyboard(),
        )

        return

    # -----------------------------------------------------
    # START SHOPPING
    # -----------------------------------------------------

    if data == "start_shopping":

        reset_order(user_id)

        order = get_order(user_id)
        order.state = NAME

        await query.message.edit_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "👤 لطفاً نام خود را وارد کنید:",
            reply_markup=cancel_keyboard(),
        )

        return

    # -----------------------------------------------------
    # ADD PRODUCT
    # -----------------------------------------------------

    if data == "add_product":

        order.state = PRODUCT

        await query.message.reply_text(
            "🛍 محصول بعدی را ارسال کنید.",
            reply_markup=product_keyboard(),
        )

        return

    # -----------------------------------------------------
    # CONTINUE ORDER
    # -----------------------------------------------------

    if data == "continue_order":

        if not order.cart:
            await query.message.reply_text(
                "⚠️ هنوز محصولی به سبد اضافه نشده است."
            )
            return

        order.state = ADDRESS

        await query.message.reply_text(
            "📍 لطفاً آدرس کامل خود را در یک پیام ارسال کنید.",
            reply_markup=cancel_keyboard(),
        )

        return

    # -----------------------------------------------------
    # SKIP PRODUCT DETAILS
    # -----------------------------------------------------

    if data == "skip_product_details":

        if not order.cart:
            await query.message.reply_text(
                "⚠️ محصولی وجود ندارد."
            )
            return

        order.cart[-1].details = ""
        order.state = CART

        await query.message.reply_text(
            "🛒 محصول به سبد خرید اضافه شد.",
            reply_markup=cart_keyboard(),
        )

        return

    # -----------------------------------------------------
    # SHIPPING
    # -----------------------------------------------------

    if data == "shipping_post":

        order.shipping_method = SHIPPING_POST
        order.state = CUSTOMER_DESCRIPTION

        await query.message.reply_text(
            "📦 روش ارسال: پست\n\n"
            "📝 اگر توضیح دیگری درباره سفارش دارید ارسال کنید.\n"
            "اگر ندارید، گزینه «بدون توضیحات» را بزنید.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⏭ بدون توضیحات",
                        callback_data="skip_customer_description",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ لغو سفارش",
                        callback_data="cancel_order",
                    )
                ],
            ]),
        )

        return

    if data == "shipping_pickup":

        order.shipping_method = SHIPPING_PICKUP
        order.state = CUSTOMER_DESCRIPTION

        await query.message.reply_text(
            "🏪 روش دریافت: دریافت توسط مشتری\n\n"
            "📝 اگر توضیح دیگری درباره سفارش دارید ارسال کنید.\n"
            "اگر ندارید، گزینه «بدون توضیحات» را بزنید.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⏭ بدون توضیحات",
                        callback_data="skip_customer_description",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ لغو سفارش",
                        callback_data="cancel_order",
                    )
                ],
            ]),
        )

        return

    # -----------------------------------------------------
    # CUSTOMER DESCRIPTION
    # -----------------------------------------------------

    if data == "skip_customer_description":

        order.customer_description = ""
        order.state = PRICE_STATUS

        await query.message.reply_text(
            "💰 وضعیت قیمت محصول را مشخص کنید:",
            reply_markup=price_keyboard(),
        )

        return

    # -----------------------------------------------------
    # PRICE KNOWN
    # -----------------------------------------------------

    if data == "price_known":

        order.price_known = True
        order.state = PAYMENT

        await ask_price(update)

        return

    # -----------------------------------------------------
    # PRICE UNKNOWN
    # -----------------------------------------------------

    if data == "price_unknown":

        order.price_known = False

        await send_price_request_to_admin(
            user_id=user_id,
            order=order,
        )

        return

    # -----------------------------------------------------
    # SEND RECEIPT BUTTON
    # -----------------------------------------------------

    if data == "send_receipt":

        if not order.price:
            await query.message.reply_text(
                "⚠️ هنوز مبلغ سفارش مشخص نشده است."
            )
            return

        order.state = RECEIPT

        await query.message.reply_text(
            "📸 لطفاً عکس یا فایل رسید پرداخت را ارسال کنید.",
            reply_markup=cancel_keyboard(),
        )

        return

    # -----------------------------------------------------
    # CONFIRM ORDER
    # -----------------------------------------------------

    if data == "confirm_order":

        if not order.receipt_file_id:
            await query.message.reply_text(
                "⚠️ رسید پرداخت هنوز دریافت نشده است."
            )
            return

        try:
            success = await send_final_order_to_admin(
                user_id=user_id,
                order=order,
            )

        except Exception as exc:
            logger.exception(
                "Error while sending final order: %s",
                exc,
            )

            success = False

        if not success:

            await query.message.reply_text(
                "⚠️ هنگام ارسال سفارش به ادمین مشکلی ایجاد شد.\n"
                "اطلاعات سفارش پاک نشده است.\n\n"
                "لطفاً دوباره روی تأیید نهایی بزنید."
            )

            return

        order.state = START

        await query.message.edit_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "✅ سفارش شما با موفقیت ثبت شد.\n\n"
            f"🔖 شماره سفارش: <code>{order.order_id}</code>\n\n"
            "ممنون از خرید شما 🌷",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # EDIT ORDER
    # -----------------------------------------------------

    if data == "edit_order":

        reset_order(user_id)

        order = get_order(user_id)
        order.state = NAME

        await query.message.edit_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "✏️ اطلاعات قبلی حذف شد.\n\n"
            "👤 لطفاً نام خود را دوباره وارد کنید:",
            reply_markup=cancel_keyboard(),
        )

        return


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
    # ADMIN ROUTING
    # -----------------------------------------------------

    if user_id == ADMIN_ID:
        await handle_admin_message(
            update,
            context,
        )
        return

    order = get_order(user_id)

    message = update.message
    text = normalize_text(message.text or "")

    # =====================================================
    # NAME
    # =====================================================

    if order.state == NAME:

        if not text:
            await message.reply_text(
                "⚠️ لطفاً نام خود را به صورت متنی وارد کنید."
            )
            return

        order.first_name = text
        order.state = PHONE

        await ask_last_name(update)

        return

    # =====================================================
    # PHONE
    # =====================================================

    if order.state == PHONE:

        # Contact
        if message.contact:

            contact = message.contact

            if contact.user_id and contact.user_id != user_id:
                await message.reply_text(
                    "⚠️ لطفاً شماره خودتان را ارسال کنید."
                )
                return

            order.phone = normalize_digits(
                contact.phone_number
            )

        else:

            phone = normalize_digits(text)

            phone = phone.replace(
                " ",
                ""
            ).replace(
                "-",
                ""
            )

            if not re.fullmatch(
                r"^\+?\d{10,15}$",
                phone
            ):
                await message.reply_text(
                    "⚠️ شماره موبایل معتبر نیست.\n"
                    "مثلاً 09121234567"
                )
                return

            order.phone = phone

        order.state = PRODUCT

        await message.reply_text(
            "✅ شماره ثبت شد.\n\n"
            "🛍 حالا محصول موردنظر را ارسال کنید.\n\n"
            "می‌توانید:\n"
            "• عکس محصول\n"
            "• فایل محصول\n"
            "• کد یا نام محصول",
            reply_markup=product_keyboard(),
        )

        return

    # =====================================================
    # PRODUCT
    # =====================================================

    if order.state == PRODUCT:

        photo_file_id = get_photo_file_id(message)
        document_file_id = get_document_file_id(message)

        if photo_file_id:

            item = CartItem(
                product_type="photo",
                product_file_id=photo_file_id,
            )

            order.cart.append(item)

            order.state = PRODUCT_DETAILS

            await message.reply_text(
                "📷 عکس محصول دریافت شد.\n\n"
                "📝 اگر جزئیاتی مثل سایز، رنگ یا تعداد دارید ارسال کنید.",
                reply_markup=product_details_keyboard(),
            )

            return

        if document_file_id:

            item = CartItem(
                product_type="document",
                product_file_id=document_file_id,
            )

            order.cart.append(item)

            order.state = PRODUCT_DETAILS

            await message.reply_text(
                "📄 فایل محصول دریافت شد.\n\n"
                "📝 اگر جزئیاتی مثل سایز، رنگ یا تعداد دارید ارسال کنید.",
                reply_markup=product_details_keyboard(),
            )

            return

        if text:

            item = CartItem(
                product_type="text",
                product_text=text,
            )

            order.cart.append(item)

            order.state = PRODUCT_DETAILS

            await message.reply_text(
                "✅ محصول دریافت شد.\n\n"
                "📝 اگر جزئیاتی مثل سایز، رنگ یا تعداد دارید ارسال کنید.",
                reply_markup=product_details_keyboard(),
            )

            return

        await message.reply_text(
            "⚠️ لطفاً عکس، فایل یا کد/نام محصول را ارسال کنید."
        )

        return

    # =====================================================
    # PRODUCT DETAILS
    # =====================================================

    if order.state == PRODUCT_DETAILS:

        if not order.cart:
            order.state = PRODUCT

            await ask_product(update)

            return

        if not text:

            await message.reply_text(
                "⚠️ لطفاً توضیحات را به صورت متنی ارسال کنید."
            )

            return

        order.cart[-1].details = text
        order.state = CART

        await message.reply_text(
            "✅ جزئیات محصول ثبت شد.",
            reply_markup=cart_keyboard(),
        )

        return

    # =====================================================
    # CART
    # =====================================================

    if order.state == CART:

        await message.reply_text(
            "از دکمه‌های زیر استفاده کنید:",
            reply_markup=cart_keyboard(),
        )

        return

    # =====================================================
    # ADDRESS
    # =====================================================

    if order.state == ADDRESS:

        if not text:

            await message.reply_text(
                "⚠️ لطفاً آدرس را به صورت متنی ارسال کنید."
            )

            return

        if len(text) < 10:

            await message.reply_text(
                "⚠️ آدرس واردشده خیلی کوتاه است.\n"
                "لطفاً آدرس کامل را ارسال کنید."
            )

            return

        order.full_address = text
        order.state = SHIPPING

        await ask_shipping(update)

        return

    # =====================================================
    # CUSTOMER DESCRIPTION
    # =====================================================

    if order.state == CUSTOMER_DESCRIPTION:

        order.customer_description = text
        order.state = PRICE_STATUS

        await ask_price_status(update)

        return

    # =====================================================
    # PRICE
    # =====================================================

    if order.state == PAYMENT:

        price = normalize_price(text)

        if price is None:

            await message.reply_text(
                "⚠️ مبلغ نامعتبر است.\n\n"
                "لطفاً فقط عدد وارد کنید.\n"
                "مثال:\n"
                "850000"
            )

            return

        order.price = price
        order.state = RECEIPT

        await ask_receipt(update)

        return

    # =====================================================
    # RECEIPT
    # =====================================================

    if order.state == RECEIPT:

        photo_file_id = get_photo_file_id(message)
        document_file_id = get_document_file_id(message)

        if photo_file_id:

            order.receipt_file_id = photo_file_id
            order.receipt_type = "photo"

        elif document_file_id:

            order.receipt_file_id = document_file_id
            order.receipt_type = "document"

        else:

            await message.reply_text(
                "⚠️ رسید دریافت نشد.\n\n"
                "لطفاً عکس یا فایل رسید پرداخت را ارسال کنید."
            )

            return

        # -------------------------------------------------
        # VERY IMPORTANT:
        # Send complete order information ONCE
        # BEFORE confirmation.
        # -------------------------------------------------

        order.state = CONFIRM

        summary = build_customer_order_summary(
            order
        )

        await message.reply_text(
            summary,
            reply_markup=confirm_keyboard(),
            parse_mode="HTML",
        )

        return

    # =====================================================
    # WAITING ADMIN PRICE
    # =====================================================

    if order.state == WAITING_ADMIN_PRICE:

        await message.reply_text(
            "⏳ درخواست قیمت شما برای ادمین ارسال شده است.\n"
            "لطفاً تا دریافت قیمت صبر کنید."
        )

        return

    # =====================================================
    # CONFIRM
    # =====================================================

    if order.state == CONFIRM:

        await message.reply_text(
            "🔎 لطفاً از دکمه زیر برای تأیید نهایی استفاده کنید.",
            reply_markup=confirm_keyboard(),
        )

        return

    # =====================================================
    # START / DEFAULT
    # =====================================================

    if order.state == START:

        await message.reply_text(
            "برای شروع خرید روی دکمه زیر بزنید.",
            reply_markup=main_keyboard(),
        )

        return


# =========================================================
# ADMIN PRICE REQUEST
# =========================================================

async def send_price_request_to_admin(
    user_id: int,
    order: OrderData,
):
    text = (
        "💰 <b>درخواست قیمت جدید</b>\n"
        "\n"
        f"🔖 <b>شماره سفارش:</b> "
        f"<code>{order.order_id}</code>\n"
        f"👤 <b>مشتری:</b> "
        f"{order.first_name} {order.last_name}\n"
        f"📱 <b>شماره:</b> {order.phone}\n"
        f"🆔 <b>Telegram ID:</b> "
        f"<code>{user_id}</code>\n"
        "\n"
        f"{build_cart_text(order)}\n"
        "\n"
        "⬅️ برای تعیین قیمت، روی همین پیام Reply کنید "
        "و مبلغ را بفرستید.\n\n"
        "مثال:\n"
        "<code>850000</code>"
    )

    try:

        admin_message = await context_bot_send_message(
            ADMIN_ID,
            text,
        )

    except Exception as exc:

        logger.exception(
            "Could not send price request to admin: %s",
            exc,
        )

        # پیام خطا به مشتری از طریق Application در تابع پایین
        # ارسال خواهد شد.
        raise

    admin_message_id = admin_message.message_id

    order.admin_price_message_id = admin_message_id
    order.waiting_for_admin_price = True
    order.state = WAITING_ADMIN_PRICE

    admin_price_requests[admin_message_id] = {
        "user_id": user_id,
        "order_id": order.order_id,
    }

    return admin_message


# =========================================================
# GLOBAL BOT REFERENCE
# =========================================================

application: Optional[Application] = None


async def context_bot_send_message(
    chat_id: int,
    text: str,
    **kwargs,
):
    if application is None:
        raise RuntimeError(
            "Application هنوز ساخته نشده است."
        )

    return await application.bot.send_message(
        chat_id=chat_id,
        text=text,
        **kwargs,
    )


# =========================================================
# ADMIN HANDLER
# =========================================================

async def handle_admin_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message

    if not message:
        return

    text = normalize_text(
        message.text or ""
    )

    # -----------------------------------------------------
    # LEGACY COMMAND
    #
    # /price USER_ID PRICE
    # -----------------------------------------------------

    if text.startswith("/price"):

        parts = text.split()

        if len(parts) != 3:

            await message.reply_text(
                "فرمت صحیح:\n"
                "/price USER_ID PRICE\n\n"
                "مثال:\n"
                "/price 123456789 850000"
            )

            return

        try:
            customer_id = int(parts[1])
        except ValueError:

            await message.reply_text(
                "❌ USER_ID نامعتبر است."
            )

            return

        price = normalize_price(parts[2])

        if price is None:

            await message.reply_text(
                "❌ مبلغ نامعتبر است."
            )

            return

        order = orders.get(customer_id)

        if not order:

            await message.reply_text(
                "❌ سفارشی برای این مشتری پیدا نشد."
            )

            return

        if not order.waiting_for_admin_price:

            await message.reply_text(
                "⚠️ این سفارش در انتظار قیمت نیست."
            )

            return

        await deliver_admin_price(
            admin_message=message,
            customer_id=customer_id,
            order=order,
            price=price,
        )

        return

    # -----------------------------------------------------
    # REPLY BASED PRICE
    # -----------------------------------------------------

    replied_message_id = get_replied_message_id(
        message
    )

    request = None

    if replied_message_id is not None:
        request = admin_price_requests.get(
            replied_message_id
        )

    customer_id = None
    order = None

    # -----------------------------------------------------
    # METHOD 1:
    # Message ID mapping
    # -----------------------------------------------------

    if request:

        customer_id = request["user_id"]

        order_id = request["order_id"]

        order = orders.get(customer_id)

        if order and order.order_id != order_id:

            customer_id = None
            order = None

    # -----------------------------------------------------
    # METHOD 2:
    # Parse order ID from replied message
    # -----------------------------------------------------

    if order is None:

        replied_message = get_replied_message(
            message
        )

        replied_text = get_message_text(
            replied_message
        )

        order_id = extract_order_id(
            replied_text
        )

        if order_id:

            customer_id, order = find_order_by_order_id(
                order_id
            )

    # -----------------------------------------------------
    # No order found
    # -----------------------------------------------------

    if order is None or customer_id is None:

        # فقط پیام‌هایی که احتمالاً قیمت هستند را هندل کنیم
        if text and normalize_price(text):

            await message.reply_text(
                "⚠️ این پیام به درخواست قیمت هیچ سفارشی "
                "متصل نیست.\n\n"
                "لطفاً قیمت را با Reply روی پیام "
                "«درخواست قیمت جدید» ارسال کنید."
            )

        return

    # -----------------------------------------------------
    # Order exists
    # -----------------------------------------------------

    if not order.waiting_for_admin_price:

        await message.reply_text(
            "⚠️ این سفارش دیگر در انتظار قیمت نیست."
        )

        return

    price = normalize_price(text)

    if price is None:

        await message.reply_text(
            "❌ مبلغ نامعتبر است.\n\n"
            "لطفاً فقط عدد وارد کنید.\n"
            "مثال:\n"
            "850000"
        )

        return

    await deliver_admin_price(
        admin_message=message,
        customer_id=customer_id,
        order=order,
        price=price,
    )


# =========================================================
# DELIVER ADMIN PRICE TO CUSTOMER
# =========================================================

async def deliver_admin_price(
    admin_message,
    customer_id: int,
    order: OrderData,
    price: str,
):

    try:

        await application.bot.send_message(
            chat_id=customer_id,
            text=(
                "💰 <b>قیمت سفارش شما مشخص شد.</b>\n\n"
                f"🔖 شماره سفارش: "
                f"<code>{order.order_id}</code>\n"
                f"💵 مبلغ: <b>{price} تومان</b>\n\n"
                "لطفاً مبلغ را پرداخت کنید و سپس "
                "رسید پرداخت را ارسال کنید."
            ),
            reply_markup=receipt_keyboard(),
            parse_mode="HTML",
        )

    except TelegramError as exc:

        logger.exception(
            "Could not send price to customer: %s",
            exc,
        )

        await admin_message.reply_text(
            "❌ ارسال قیمت برای مشتری انجام نشد.\n"
            "سفارش همچنان در وضعیت انتظار قیمت باقی ماند."
        )

        return False

    # فقط بعد از ارسال موفق به مشتری
    # وضعیت سفارش را تغییر می‌دهیم.

    order.price = price
    order.waiting_for_admin_price = False
    order.state = RECEIPT

    cleanup_price_request(order)

    await admin_message.reply_text(
        "✅ قیمت با موفقیت برای مشتری ارسال شد.\n\n"
        f"🔖 سفارش: <code>{order.order_id}</code>\n"
        f"💰 مبلغ: <b>{price} تومان</b>",
        parse_mode="HTML",
    )

    return True


# =========================================================
# FINAL ORDER -> ADMIN
# =========================================================

async def send_final_order_to_admin(
    user_id: int,
    order: OrderData,
) -> bool:

    # -----------------------------------------------------
    # 1. Send full order summary
    # -----------------------------------------------------

    admin_text = build_admin_order_summary(
        order,
        user_id,
    )

    try:

        await application.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text,
            parse_mode="HTML",
        )

    except TelegramError as exc:

        logger.exception(
            "Could not send order summary: %s",
            exc,
        )

        return False

    # -----------------------------------------------------
    # 2. Send product media
    # -----------------------------------------------------

    for index, item in enumerate(
        order.cart,
        start=1
    ):

        if not item.product_file_id:
            continue

        try:

            if item.product_type == "photo":

                caption = (
                    f"🛍 محصول شماره {index}\n"
                    f"🔖 سفارش: {order.order_id}"
                )

                await application.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=item.product_file_id,
                    caption=caption,
                )

            elif item.product_type == "document":

                caption = (
                    f"📄 فایل محصول شماره {index}\n"
                    f"🔖 سفارش: {order.order_id}"
                )

                await application.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=item.product_file_id,
                    caption=caption,
                )

        except TelegramError as exc:

            logger.exception(
                "Could not send product media: %s",
                exc,
            )

            await application.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "⚠️ ارسال یکی از فایل‌های محصول "
                    "با خطا مواجه شد.\n\n"
                    f"🔖 سفارش: {order.order_id}\n"
                    f"❌ خطا: {exc}"
                ),
            )

    # -----------------------------------------------------
    # 3. Send receipt
    # -----------------------------------------------------

    if order.receipt_file_id:

        try:

            receipt_caption = (
                "💳 رسید پرداخت\n"
                f"🔖 سفارش: {order.order_id}\n"
                f"👤 Telegram ID: {user_id}"
            )

            if order.receipt_type == "photo":

                await application.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=order.receipt_file_id,
                    caption=receipt_caption,
                )

            elif order.receipt_type == "document":

                await application.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=order.receipt_file_id,
                    caption=receipt_caption,
                )

            else:

                # fallback
                await application.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=order.receipt_file_id,
                    caption=receipt_caption,
                )

        except TelegramError as exc:

            logger.exception(
                "Could not send receipt: %s",
                exc,
            )

            await application.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "❌ <b>رسید پرداخت ارسال نشد.</b>\n\n"
                    f"🔖 سفارش: <code>{order.order_id}</code>\n"
                    f"❌ خطا: {exc}"
                ),
                parse_mode="HTML",
            )

            return False

    # -----------------------------------------------------
    # 4. Final admin notification
    # -----------------------------------------------------

    try:

        await application.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "━━━━━━━━━━━━━━━━━━\n"
                "✅ <b>سفارش کامل دریافت شد.</b>\n"
                f"🔖 شماره سفارش: "
                f"<code>{order.order_id}</code>\n"
                "━━━━━━━━━━━━━━━━━━"
            ),
            parse_mode="HTML",
        )

    except TelegramError as exc:

        logger.exception(
            "Could not send final admin notification: %s",
            exc,
        )

        # سفارش قبلاً ارسال شده، بنابراین موفق محسوب می‌شود.

    return True


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Unhandled exception:",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    global application

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # -----------------------------------------------------
    # COMMANDS
    # -----------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    # -----------------------------------------------------
    # CALLBACK QUERIES
    # -----------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # -----------------------------------------------------
    # CUSTOMER + ADMIN MESSAGES
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            customer_message_handler,
        )
    )

    # -----------------------------------------------------
    # ERROR
    # -----------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    print(
        "========================================"
    )
    print(
        "Suryan Telegram Bot is running..."
    )
    print(
        f"Admin ID: {ADMIN_ID}"
    )
    print(
        "========================================"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()