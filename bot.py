import re
import uuid
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from html import escape
import os
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
# فقط این 3 مورد را تنظیم کن
# =========================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CARD_NUMBER = os.getenv("CARD_NUMBER")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
# =========================================================
# CONFIG
# =========================================================

SHIPPING_POST = "پست"
SHIPPING_PICKUP = "دریافت توسط مشتری"


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


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
# DATA
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
# MEMORY
# =========================================================

orders: Dict[int, OrderData] = {}

# admin_message_id -> {
#     "user_id": customer Telegram ID,
#     "order_id": order ID
# }
admin_price_requests: Dict[int, Dict] = {}


# =========================================================
# HELPERS
# =========================================================

def normalize_text(value: str) -> str:
    if not value:
        return ""

    return (
        str(value)
        .strip()
        .replace("ي", "ی")
        .replace("ى", "ی")
        .replace("ك", "ک")
    )


def normalize_digits(value: str) -> str:
    value = str(value or "")

    translation = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )

    return value.translate(translation)


def normalize_price(value: str) -> Optional[str]:
    if not value:
        return None

    value = normalize_digits(value)

    value = (
        value
        .replace(",", "")
        .replace("٬", "")
        .replace("،", "")
        .replace(" ", "")
        .replace("تومان", "")
        .replace("تومن", "")
        .replace("ریال", "")
    )

    if not value.isdigit():
        return None

    number = int(value)

    if number <= 0:
        return None

    return f"{number:,} تومان"


def get_user_id(update: Update) -> Optional[int]:
    if update.effective_user:
        return update.effective_user.id

    return None


def get_text(update: Update) -> str:
    if not update.message:
        return ""

    return normalize_text(
        update.message.text or ""
    )


def get_order(user_id: int) -> OrderData:
    if user_id not in orders:
        orders[user_id] = OrderData()

    return orders[user_id]


def cleanup_price_request(message_id: Optional[int]):
    if message_id is None:
        return

    admin_price_requests.pop(
        message_id,
        None
    )


def cleanup_price_requests_for_user(user_id: int):
    to_delete = []

    for message_id, data in list(admin_price_requests.items()):
        if data.get("user_id") == user_id:
            to_delete.append(message_id)

    for message_id in to_delete:
        admin_price_requests.pop(
            message_id,
            None
        )


def reset_order(user_id: int):
    old_order = orders.get(user_id)

    if old_order:
        cleanup_price_requests_for_user(user_id)

    orders[user_id] = OrderData()

    return orders[user_id]


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


def find_order_by_order_id(order_id: str):
    if not order_id:
        return None, None

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
        r"شماره\s*سفارش\s*[:：]?\s*([A-Z0-9\-]+)",
        r"ORDER\s*ID\s*[:：]?\s*([A-Z0-9\-]+)",
        r"(S-[A-Z0-9\-]+)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            return match.group(1).upper()

    return None


def get_replied_message_id(update: Update) -> Optional[int]:
    if not update.message:
        return None

    replied = update.message.reply_to_message

    if not replied:
        return None

    return replied.message_id


# =========================================================
# INLINE KEYBOARDS
# تمام دکمه‌های کاربر زیر همان پیام نمایش داده می‌شوند.
# =========================================================

def inline_keyboard(rows):
    return InlineKeyboardMarkup(rows)


def main_start_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "🛍 شروع خرید",
                callback_data="start_shopping"
            )
        ]
    ])


def cancel_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order"
            )
        ]
    ])


def product_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "➕ افزودن محصول",
                callback_data="add_product"
            )
        ],
        [
            InlineKeyboardButton(
                "🛒 مشاهده سبد خرید",
                callback_data="show_cart"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order"
            )
        ]
    ])


def cart_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "➕ افزودن محصول",
                callback_data="add_product"
            )
        ],
        [
            InlineKeyboardButton(
                "🛒 مشاهده سبد خرید",
                callback_data="show_cart"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ ادامه ثبت سفارش",
                callback_data="continue_order"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order"
            )
        ]
    ])


def product_details_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "⏭ بدون توضیح",
                callback_data="skip_product_details"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order"
            )
        ]
    ])


def shipping_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "📦 پست",
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
    ])


def customer_description_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "⏭ بدون توضیح",
                callback_data="skip_customer_description"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order"
            )
        ]
    ])


def price_keyboard():
    return inline_keyboard([
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
    ])


def payment_keyboard():
    return inline_keyboard([
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
        ]
    ])


def confirm_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "✅ تأیید نهایی سفارش",
                callback_data="confirm_order"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ ویرایش سفارش",
                callback_data="edit_order"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order"
            )
        ]
    ])


# =========================================================
# CART
# =========================================================

def cart_text(order: OrderData) -> str:
    if not order.cart:
        return "🛒 سبد خرید شما خالی است."

    lines = [
        "🛒 سبد خرید شما:",
        ""
    ]

    for index, item in enumerate(order.cart, start=1):

        if item.product_type == "photo":
            product_name = "📷 محصول تصویری"

        elif item.product_type == "document":
            product_name = "📄 محصول ارسالی"

        else:
            product_name = (
                item.product_text
                or "محصول"
            )

        lines.append(
            f"{index}. {escape(product_name)}"
        )

        if item.details:
            lines.append(
                f"   📝 {escape(item.details)}"
            )

        lines.append("")

    return "\n".join(lines)


# =========================================================
# CUSTOMER ORDER SUMMARY
# =========================================================

def customer_order_summary(order: OrderData) -> str:
    customer_name = (
        f"{order.first_name} "
        f"{order.last_name}"
    ).strip()

    return (
        "🧾 <b>اطلاعات کامل سفارش شما</b>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"

        f"🆔 <b>شماره سفارش:</b>\n"
        f"<code>{escape(order.order_id)}</code>\n\n"

        "👤 <b>اطلاعات مشتری</b>\n"
        f"نام: {escape(customer_name)}\n"
        f"📱 شماره تماس: {escape(order.phone)}\n\n"

        "🛒 <b>محصولات</b>\n"
        f"{cart_text(order)}\n"

        "📍 <b>آدرس</b>\n"
        f"{escape(order.full_address)}\n\n"

        "🚚 <b>روش دریافت</b>\n"
        f"{escape(order.shipping_method)}\n\n"

        "💰 <b>مبلغ سفارش</b>\n"
        f"{escape(order.price)}\n\n"

        "📝 <b>توضیحات</b>\n"
        f"{escape(order.customer_description or 'ندارد')}\n\n"

        "💳 <b>وضعیت پرداخت</b>\n"
        "رسید پرداخت دریافت شد\n\n"

        "━━━━━━━━━━━━━━━━━━\n\n"

        "⚠️ لطفاً تمام اطلاعات بالا را بررسی کنید.\n"
        "اگر همه اطلاعات صحیح است، روی "
        "«✅ تأیید نهایی سفارش» بزنید.\n\n"
        "در صورت وجود اشتباه، "
        "«✏️ ویرایش سفارش» را انتخاب کنید."
    )


# =========================================================
# ADMIN SUMMARY
# =========================================================

def admin_order_summary(
    user_id: int,
    order: OrderData
) -> str:

    customer_name = (
        f"{order.first_name} "
        f"{order.last_name}"
    ).strip()

    return (
        "🛍 <b>سفارش جدید سورین</b>\n\n"

        f"🆔 <b>شماره سفارش:</b> "
        f"<code>{escape(order.order_id)}</code>\n"

        f"👤 <b>مشتری:</b> "
        f"{escape(customer_name)}\n"

        f"📱 <b>شماره تماس:</b> "
        f"{escape(order.phone)}\n"

        f"🆔 <b>Telegram ID:</b> "
        f"<code>{user_id}</code>\n\n"

        "━━━━━━━━━━━━━━━━━━\n\n"

        "🛒 <b>محصولات</b>\n"
        f"{cart_text(order)}\n"

        "📍 <b>آدرس کامل</b>\n"
        f"{escape(order.full_address)}\n\n"

        "🚚 <b>روش دریافت</b>\n"
        f"{escape(order.shipping_method)}\n\n"

        "💰 <b>قیمت</b>\n"
        f"{escape(order.price)}\n\n"

        "📝 <b>توضیحات مشتری</b>\n"
        f"{escape(order.customer_description or 'ندارد')}\n\n"

        "💳 <b>وضعیت پرداخت:</b> "
        "رسید ارسال شده"
    )


# =========================================================
# CUSTOMER PROMPTS
# =========================================================

async def send_welcome(
    chat_id: int,
    bot
):
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "🛍 به فروشگاه سورین خوش آمدید!\n\n"
            "برای ثبت سفارش، روی دکمه "
            "«🛍 شروع خرید» بزنید."
        ),
        reply_markup=main_start_keyboard()
    )


async def ask_name(
    chat_id: int,
    bot,
    intro: bool = False
):
    text = (
        "عالیه 🌱\n\n"
        "برای شروع ثبت سفارش، لطفاً "
        "<b>نام و نام خانوادگی</b> خود را "
        "در یک پیام وارد کنید.\n\n"
        "مثال:\n"
        "پرهام جانجان"
        if intro
        else
        "👤 لطفاً نام و نام خانوادگی خود را "
        "در یک پیام وارد کنید."
    )

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )


async def ask_phone(
    chat_id: int,
    bot
):
    # Telegram برای Inline Keyboard امکان request_contact ندارد.
    # بنابراین شماره به‌صورت دستی دریافت می‌شود تا تمام دکمه‌ها
    # همچنان دقیقاً زیر پیام باقی بمانند.
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "📱 لطفاً شماره تماس خود را ارسال کنید.\n\n"
            "مثال:\n"
            "09121234567"
        ),
        reply_markup=cancel_keyboard()
    )


async def ask_product(
    chat_id: int,
    bot
):
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "🛍 حالا محصول موردنظر را ارسال کنید.\n\n"
            "می‌توانید یکی از این موارد را بفرستید:\n"
            "• عکس محصول\n"
            "• کد محصول\n"
            "• نام محصول\n"
            "• لینک محصول\n\n"
            "بعد از ارسال محصول، اطلاعات تکمیلی آن را "
            "از شما می‌پرسم."
        ),
        reply_markup=product_keyboard()
    )


async def ask_product_details(
    chat_id: int,
    bot
):
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "📝 اگر برای این محصول توضیحی دارید، "
            "همینجا بنویسید.\n\n"
            "مثلاً:\n"
            "سایز L، رنگ مشکی\n\n"
            "اگر توضیحی ندارید، روی "
            "«⏭ بدون توضیح» بزنید."
        ),
        reply_markup=product_details_keyboard()
    )


async def ask_address(
    chat_id: int,
    bot
):
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "📍 لطفاً آدرس کامل را در یک پیام ارسال کنید.\n\n"
            "ساختار پیشنهادی:\n\n"
            "استان: ...\n"
            "شهر: ...\n"
            "آدرس دقیق: ...\n"
            "کدپستی: ...\n\n"
            "مثال:\n\n"
            "استان: البرز\n"
            "شهر: کرج\n"
            "آدرس دقیق: مهرشهر، بلوار ارم، خیابان ...، "
            "پلاک ۱۲، واحد ۳\n"
            "کدپستی: ۳۱۸۷۶۴۵۱۲۳\n\n"
            "⚠️ اگر کدپستی را دارید، آن را در آخر آدرس بنویسید.\n"
            "اگر کدپستی ندارید، فقط آدرس کامل را ارسال کنید."
        ),
        reply_markup=cancel_keyboard()
    )


async def ask_shipping(
    chat_id: int,
    bot
):
    await bot.send_message(
        chat_id=chat_id,
        text="🚚 روش دریافت سفارش را انتخاب کنید:",
        reply_markup=shipping_keyboard()
    )


async def ask_customer_description(
    chat_id: int,
    bot
):
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "📝 اگر توضیح یا درخواست خاصی برای سفارش دارید، "
            "بنویسید.\n\n"
            "مثلاً:\n"
            "لطفاً قبل از ارسال با من تماس بگیرید.\n\n"
            "اگر توضیحی ندارید، روی "
            "«⏭ بدون توضیح» بزنید."
        ),
        reply_markup=customer_description_keyboard()
    )


async def ask_price_status(
    chat_id: int,
    bot
):
    await bot.send_message(
        chat_id=chat_id,
        text="💰 آیا قیمت محصول را می‌دانید؟",
        reply_markup=price_keyboard()
    )


async def ask_price(
    chat_id: int,
    bot
):
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "💰 لطفاً مبلغ سفارش را وارد کنید.\n\n"
            "مثال:\n"
            "850000"
        ),
        reply_markup=cancel_keyboard()
    )


async def ask_payment(
    chat_id: int,
    bot,
    order: OrderData
):
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "💳 مبلغ سفارش شما:\n\n"
            f"💰 <b>{escape(order.price)}</b>\n\n"
            "لطفاً مبلغ بالا را به شماره کارت زیر "
            "واریز کنید:\n\n"
            f"<code>{escape(CARD_NUMBER)}</code>\n\n"
            "بعد از پرداخت، تصویر رسید پرداخت را ارسال کنید."
        ),
        parse_mode="HTML",
        reply_markup=payment_keyboard()
    )


async def ask_receipt(
    chat_id: int,
    bot
):
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "📸 لطفاً تصویر رسید پرداخت را ارسال کنید.\n\n"
            "می‌توانید رسید را به‌صورت عکس یا فایل PDF "
            "ارسال کنید."
        ),
        reply_markup=cancel_keyboard()
    )


# =========================================================
# START SHOPPING
# =========================================================

async def start_purchase(
    chat_id: int,
    bot
):
    reset_order(chat_id)

    order = get_order(chat_id)
    order.state = NAME

    await ask_name(
        chat_id,
        bot,
        intro=True
    )


# =========================================================
# CART DISPLAY
# =========================================================

async def show_cart(
    chat_id: int,
    bot
):
    order = get_order(chat_id)

    await bot.send_message(
        chat_id=chat_id,
        text=cart_text(order),
        parse_mode="HTML",
        reply_markup=cart_keyboard()
    )


# =========================================================
# PRICE REQUEST TO ADMIN
# =========================================================

async def send_price_request_to_admin(
    user_id: int,
    order: OrderData,
    bot
):
    customer_name = (
        f"{order.first_name} "
        f"{order.last_name}"
    ).strip()

    text = (
        "💰 <b>درخواست تعیین قیمت جدید</b>\n\n"

        f"🆔 <b>شماره سفارش:</b> "
        f"<code>{escape(order.order_id)}</code>\n"

        f"👤 <b>مشتری:</b> "
        f"{escape(customer_name)}\n"

        f"📱 <b>شماره تماس:</b> "
        f"{escape(order.phone)}\n\n"

        f"🛒 <b>تعداد محصولات:</b> "
        f"{len(order.cart)}\n\n"

        "📦 <b>سبد خرید:</b>\n"
        f"{cart_text(order)}\n"

        "📍 <b>آدرس:</b>\n"
        f"{escape(order.full_address)}\n\n"

        f"🚚 <b>روش ارسال:</b> "
        f"{escape(order.shipping_method)}\n\n"

        "📝 <b>توضیحات مشتری:</b>\n"
        f"{escape(order.customer_description or 'ندارد')}\n\n"

        "━━━━━━━━━━━━━━\n"

        "⚠️ برای تعیین قیمت، روی همین پیام "
        "<b>Reply</b> کنید.\n\n"

        "فقط مبلغ را ارسال کنید:\n"
        "<code>850000</code>\n\n"

        "یا:\n"
        "<code>/price 850000</code>\n"

        "━━━━━━━━━━━━━━"
    )

    admin_message = await bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
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

    logger.info(
        "PRICE REQUEST SENT: message=%s order=%s user=%s",
        admin_message_id,
        order.order_id,
        user_id
    )


# =========================================================
# DELIVER ADMIN PRICE
# =========================================================

async def deliver_admin_price(
    user_id: int,
    order: OrderData,
    price: str,
    admin_message_id: Optional[int],
    bot
) -> bool:

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 <b>قیمت سفارش شما مشخص شد!</b>\n\n"

                f"🆔 <b>شماره سفارش:</b>\n"
                f"<code>{escape(order.order_id)}</code>\n\n"

                "━━━━━━━━━━━━━━\n"

                "💰 <b>مبلغ قابل پرداخت:</b>\n\n"
                f"💵 <b>{escape(price)}</b>\n"

                "━━━━━━━━━━━━━━\n\n"

                "💳 لطفاً مبلغ بالا را به شماره کارت زیر "
                "واریز کنید:\n\n"

                f"<code>{escape(CARD_NUMBER)}</code>\n\n"

                "📸 بعد از پرداخت، تصویر رسید پرداخت را "
                "ارسال کنید."
            ),
            parse_mode="HTML",
            reply_markup=payment_keyboard()
        )

    except TelegramError:
        logger.exception(
            "Could not send price to customer."
        )

        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "❌ قیمت ذخیره شد اما ارسال آن برای مشتری "
                "با خطا مواجه شد.\n\n"
                f"🆔 سفارش: {escape(order.order_id)}"
            ),
            parse_mode="HTML"
        )

        return False

    order.price = price
    order.price_known = True
    order.waiting_for_admin_price = False
    order.state = RECEIPT

    cleanup_price_request(admin_message_id)
    cleanup_price_request(order.admin_price_message_id)

    order.admin_price_message_id = None

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "✅ <b>قیمت با موفقیت ثبت و برای مشتری ارسال شد.</b>\n\n"
            f"🆔 سفارش: <code>{escape(order.order_id)}</code>\n"
            f"💰 مبلغ: <b>{escape(price)}</b>"
        ),
        parse_mode="HTML"
    )

    return True


# =========================================================
# ADMIN PRICE PROCESSOR
# =========================================================

async def process_admin_price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        return

    text = normalize_text(
        update.message.text or ""
    )

    if not text:
        return

    customer_id = None
    order = None

    # -----------------------------------------------------
    # اولویت اول: Reply به پیام درخواست قیمت
    # -----------------------------------------------------

    replied_id = get_replied_message_id(update)

    if replied_id is not None:

        request = admin_price_requests.get(
            replied_id
        )

        if request:

            customer_id = request.get("user_id")
            order_id = request.get("order_id")

            current_order = orders.get(
                customer_id
            )

            if (
                current_order
                and current_order.order_id == order_id
            ):
                order = current_order

        # fallback با شماره سفارش داخل پیام Reply شده
        if order is None and update.message.reply_to_message:

            replied_text = (
                update.message.reply_to_message.text
                or update.message.reply_to_message.caption
                or ""
            )

            extracted_order_id = extract_order_id(
                replied_text
            )

            if extracted_order_id:

                customer_id, order = find_order_by_order_id(
                    extracted_order_id
                )

    # -----------------------------------------------------
    # حالت /price USER_ID PRICE
    # -----------------------------------------------------

    if order is None:

        parts = text.split()

        if (
            len(parts) >= 3
            and parts[0].lower() == "/price"
        ):

            try:
                legacy_user_id = int(parts[1])

            except ValueError:
                await update.message.reply_text(
                    "❌ شناسه کاربر معتبر نیست."
                )
                return

            legacy_order = orders.get(
                legacy_user_id
            )

            if legacy_order is None:
                await update.message.reply_text(
                    "❌ سفارش فعالی برای این کاربر پیدا نشد."
                )
                return

            customer_id = legacy_user_id
            order = legacy_order

    # -----------------------------------------------------
    # سفارش پیدا نشد
    # -----------------------------------------------------

    if order is None or customer_id is None:

        await update.message.reply_text(
            "❌ سفارش مربوط به این پیام پیدا نشد.\n\n"
            "برای جلوگیری از اشتباه، روی همان پیام "
            "«💰 درخواست تعیین قیمت جدید» Reply کنید."
        )
        return

    # -----------------------------------------------------
    # وضعیت سفارش
    # -----------------------------------------------------

    if order.state != WAITING_ADMIN_PRICE:

        await update.message.reply_text(
            "❌ این سفارش دیگر در انتظار قیمت نیست.\n\n"
            f"🆔 {order.order_id}"
        )
        return

    # -----------------------------------------------------
    # استخراج قیمت
    # -----------------------------------------------------

    raw_price = text

    if text.lower().startswith("/price"):

        parts = text.split()

        if len(parts) == 2:
            raw_price = parts[1]

        elif len(parts) >= 3:
            raw_price = parts[-1]

    price = normalize_price(raw_price)

    if not price:

        await update.message.reply_text(
            "❌ قیمت معتبر نیست.\n\n"
            "مثال:\n"
            "850000"
        )
        return

    await deliver_admin_price(
        customer_id,
        order,
        price,
        replied_id or order.admin_price_message_id,
        context.bot
    )


# =========================================================
# FINAL ORDER TO ADMIN
# =========================================================

async def send_final_order_to_admin(
    user_id: int,
    order: OrderData,
    bot
) -> bool:

    # -----------------------------------------------------
    # خلاصه کامل سفارش
    # -----------------------------------------------------

    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_order_summary(
                user_id,
                order
            ),
            parse_mode="HTML"
        )

    except Exception:
        logger.exception(
            "Could not send admin order summary."
        )
        return False

    # -----------------------------------------------------
    # ارسال محصولات
    # -----------------------------------------------------

    for index, item in enumerate(
        order.cart,
        start=1
    ):

        caption = (
            f"🛍 <b>محصول {index}</b>\n"
            f"🆔 سفارش: <code>{escape(order.order_id)}</code>"
        )

        if item.details:
            caption += (
                f"\n📝 توضیحات: "
                f"{escape(item.details)}"
            )

        try:

            if (
                item.product_type == "photo"
                and item.product_file_id
            ):

                await bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=item.product_file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif (
                item.product_type == "document"
                and item.product_file_id
            ):

                await bot.send_document(
                    chat_id=ADMIN_ID,
                    document=item.product_file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

        except Exception:
            logger.exception(
                "Could not send product %s.",
                index
            )

    # -----------------------------------------------------
    # ارسال رسید
    # -----------------------------------------------------

    if (
        order.receipt_file_id
        and order.receipt_type == "photo"
    ):

        try:

            await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=order.receipt_file_id,
                caption=(
                    "🧾 <b>رسید پرداخت</b>\n\n"
                    f"🆔 سفارش: "
                    f"<code>{escape(order.order_id)}</code>\n"
                    f"👤 مشتری: "
                    f"{escape(order.first_name)} "
                    f"{escape(order.last_name)}"
                ),
                parse_mode="HTML"
            )

        except Exception:
            logger.exception(
                "Could not send receipt photo."
            )
            return False

    elif (
        order.receipt_file_id
        and order.receipt_type == "document"
    ):

        try:

            await bot.send_document(
                chat_id=ADMIN_ID,
                document=order.receipt_file_id,
                caption=(
                    "🧾 <b>رسید پرداخت</b>\n\n"
                    f"🆔 سفارش: "
                    f"<code>{escape(order.order_id)}</code>\n"
                    f"👤 مشتری: "
                    f"{escape(order.first_name)} "
                    f"{escape(order.last_name)}"
                ),
                parse_mode="HTML"
            )

        except Exception:
            logger.exception(
                "Could not send receipt document."
            )
            return False

    # -----------------------------------------------------
    # پیام نهایی ادمین
    # -----------------------------------------------------

    try:

        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "✅ <b>سفارش کامل دریافت شد.</b>\n\n"
                f"🆔 شماره سفارش: "
                f"<code>{escape(order.order_id)}</code>\n\n"
                "📦 اطلاعات سفارش، محصولات و رسید پرداخت "
                "ارسال شدند."
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
    chat_id = query.message.chat_id
    bot = context.bot

    order = get_order(user_id)
    data = query.data

    # -----------------------------------------------------
    # CANCEL
    # -----------------------------------------------------

    if data == "cancel_order":

        reset_order(user_id)

        await query.edit_message_text(
            "❌ سفارش لغو شد."
        )

        await bot.send_message(
            chat_id=chat_id,
            text="🛍 برای شروع دوباره:",
            reply_markup=main_start_keyboard()
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

        await ask_name(
            chat_id,
            bot,
            intro=True
        )

        return

    # -----------------------------------------------------
    # ADD PRODUCT
    # -----------------------------------------------------

    if data == "add_product":

        order.state = PRODUCT

        await query.edit_message_text(
            "➕ افزودن محصول جدید"
        )

        await ask_product(
            chat_id,
            bot
        )

        return

    # -----------------------------------------------------
    # SHOW CART
    # -----------------------------------------------------

    if data == "show_cart":

        await show_cart(
            chat_id,
            bot
        )

        return

    # -----------------------------------------------------
    # CONTINUE ORDER
    # -----------------------------------------------------

    if data == "continue_order":

        if not order.cart:

            await query.answer(
                "سبد خرید شما خالی است.",
                show_alert=True
            )
            return

        order.state = ADDRESS

        await query.edit_message_text(
            "➡️ ادامه ثبت سفارش"
        )

        await ask_address(
            chat_id,
            bot
        )

        return

    # -----------------------------------------------------
    # SKIP PRODUCT DETAILS
    # -----------------------------------------------------

    if data == "skip_product_details":

        if not order.cart:

            order.state = PRODUCT

            await ask_product(
                chat_id,
                bot
            )
            return

        order.cart[-1].details = ""
        order.state = CART

        await query.edit_message_text(
            "✅ توضیحات محصول ثبت نشد."
        )

        await show_cart(
            chat_id,
            bot
        )

        return

    # -----------------------------------------------------
    # SHIPPING
    # -----------------------------------------------------

    if data == "shipping_post":

        order.shipping_method = SHIPPING_POST
        order.state = CUSTOMER_DESCRIPTION

        await query.edit_message_text(
            "📦 روش دریافت: پست"
        )

        await ask_customer_description(
            chat_id,
            bot
        )

        return

    if data == "shipping_pickup":

        order.shipping_method = SHIPPING_PICKUP
        order.state = CUSTOMER_DESCRIPTION

        await query.edit_message_text(
            "🏪 روش دریافت: دریافت توسط مشتری"
        )

        await ask_customer_description(
            chat_id,
            bot
        )

        return

    # -----------------------------------------------------
    # CUSTOMER DESCRIPTION SKIP
    # -----------------------------------------------------

    if data == "skip_customer_description":

        order.customer_description = ""
        order.state = PRICE_STATUS

        await query.edit_message_text(
            "⏭ بدون توضیح"
        )

        await ask_price_status(
            chat_id,
            bot
        )

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

        await ask_price(
            chat_id,
            bot
        )

        return

    # -----------------------------------------------------
    # PRICE UNKNOWN
    # -----------------------------------------------------

    if data == "price_unknown":

        order.price_known = False

        try:

            await send_price_request_to_admin(
                user_id,
                order,
                bot
            )

        except Exception:
            logger.exception(
                "Could not send price request."
            )

            order.state = PRICE_STATUS
            order.waiting_for_admin_price = False

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ ارسال درخواست قیمت با مشکل مواجه شد.\n"
                    "لطفاً دوباره تلاش کنید."
                ),
                reply_markup=price_keyboard()
            )

            return

        await query.edit_message_text(
            "✅ اطلاعات سفارش شما برای فروشگاه ارسال شد."
        )

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "💰 قیمت سفارش توسط فروشگاه بررسی می‌شود.\n"
                "بعد از تعیین قیمت، مبلغ و اطلاعات پرداخت "
                "برای شما ارسال خواهد شد.\n\n"
                f"🆔 <b>شماره سفارش:</b>\n"
                f"<code>{escape(order.order_id)}</code>"
            ),
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )

        return

    # -----------------------------------------------------
    # SEND RECEIPT BUTTON
    # -----------------------------------------------------

    if data == "send_receipt":

        order.state = RECEIPT

        await query.edit_message_text(
            "📸 ارسال رسید پرداخت"
        )

        await ask_receipt(
            chat_id,
            bot
        )

        return

    # -----------------------------------------------------
    # FINAL CONFIRM
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
            order,
            bot
        )

        if not success:

            await query.answer(
                "ارسال سفارش با مشکل مواجه شد.",
                show_alert=True
            )
            return

        order_id = order.order_id

        order.state = START

        await query.edit_message_text(
            "🎉 <b>سفارش شما با موفقیت ثبت شد.</b>\n\n"
            f"🆔 <b>شماره سفارش:</b>\n"
            f"<code>{escape(order_id)}</code>\n\n"
            "✅ سفارش شما برای فروشگاه ارسال شد.\n"
            "در صورت نیاز، فروشگاه با شما تماس خواهد گرفت.\n\n"
            "از خرید شما از سورین ممنونیم 🌱",
            parse_mode="HTML"
        )

        await bot.send_message(
            chat_id=chat_id,
            text="🛍 برای ثبت سفارش جدید:",
            reply_markup=main_start_keyboard()
        )

        return

    # -----------------------------------------------------
    # EDIT
    # -----------------------------------------------------

    if data == "edit_order":

        reset_order(user_id)

        order = get_order(user_id)
        order.state = NAME

        await query.edit_message_text(
            "✏️ ویرایش سفارش شروع شد."
        )

        await ask_name(
            chat_id,
            bot
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

    # ادمین
    if user_id == ADMIN_ID:

        await process_admin_price(
            update,
            context
        )

        return

    text = get_text(update)
    normalized = text.lower()
    order = get_order(user_id)
    bot = context.bot
    chat_id = update.effective_chat.id

    # -----------------------------------------------------
    # START / CANCEL
    # -----------------------------------------------------

    if normalized in [
        "/start",
        "شروع"
    ]:

        await send_welcome(
            chat_id,
            bot
        )
        return

    if normalized in [
        "/cancel",
        "cancel",
        "❌ لغو سفارش",
        "لغو سفارش",
        "/reset",
        "شروع مجدد"
    ]:

        reset_order(user_id)

        await bot.send_message(
            chat_id=chat_id,
            text="❌ سفارش لغو/بازنشانی شد.\n\n🛍 برای شروع دوباره:",
            reply_markup=main_start_keyboard()
        )
        return

    # -----------------------------------------------------
    # START STATE
    # -----------------------------------------------------

    if order.state == START:

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "لطفاً برای شروع ثبت سفارش، "
                "روی دکمه «🛍 شروع خرید» بزنید."
            ),
            reply_markup=main_start_keyboard()
        )
        return

    # -----------------------------------------------------
    # NAME
    # -----------------------------------------------------

    if order.state == NAME:

        if not text:

            await bot.send_message(
                chat_id=chat_id,
                text="لطفاً نام و نام خانوادگی خود را وارد کنید.",
                reply_markup=cancel_keyboard()
            )
            return

        parts = text.split(
            maxsplit=1
        )

        order.first_name = parts[0]

        if len(parts) > 1:
            order.last_name = parts[1]
        else:
            order.last_name = ""

        order.state = PHONE

        await ask_phone(
            chat_id,
            bot
        )
        return

    # -----------------------------------------------------
    # PHONE
    # -----------------------------------------------------

    if order.state == PHONE:

        phone = normalize_digits(text)

        phone = re.sub(
            r"[\s\-\(\)]",
            "",
            phone
        )

        if not re.fullmatch(
            r"\+?\d{10,15}",
            phone
        ):

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ شماره تماس معتبر نیست.\n\n"
                    "مثال:\n"
                    "09121234567"
                ),
                reply_markup=cancel_keyboard()
            )
            return

        order.phone = phone
        order.state = PRODUCT

        await bot.send_message(
            chat_id=chat_id,
            text="✅ شماره تماس ثبت شد."
        )

        await ask_product(
            chat_id,
            bot
        )
        return

    # -----------------------------------------------------
    # PRODUCT
    # -----------------------------------------------------

    if order.state == PRODUCT:

        # دکمه‌های قدیمی/متنی هم پشتیبانی می‌شوند.
        if normalized in [
            "➕ افزودن محصول",
            "افزودن محصول"
        ]:

            await ask_product(
                chat_id,
                bot
            )
            return

        if normalized in [
            "🛒 مشاهده سبد خرید",
            "مشاهده سبد خرید"
        ]:

            await show_cart(
                chat_id,
                bot
            )
            return

        photo_file_id = get_photo_file_id(update)

        if photo_file_id:

            order.cart.append(
                CartItem(
                    product_type="photo",
                    product_file_id=photo_file_id,
                    product_text="محصول تصویری"
                )
            )

            order.state = PRODUCT_DETAILS

            await ask_product_details(
                chat_id,
                bot
            )
            return

        document_file_id = get_document_file_id(update)

        if document_file_id:

            order.cart.append(
                CartItem(
                    product_type="document",
                    product_file_id=document_file_id,
                    product_text="محصول ارسالی"
                )
            )

            order.state = PRODUCT_DETAILS

            await ask_product_details(
                chat_id,
                bot
            )
            return

        if text:

            order.cart.append(
                CartItem(
                    product_type="text",
                    product_text=text
                )
            )

            order.state = PRODUCT_DETAILS

            await ask_product_details(
                chat_id,
                bot
            )
            return

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "لطفاً عکس، کد، نام یا لینک محصول را ارسال کنید."
            ),
            reply_markup=product_keyboard()
        )
        return

    # -----------------------------------------------------
    # PRODUCT DETAILS
    # -----------------------------------------------------

    if order.state == PRODUCT_DETAILS:

        if not order.cart:

            order.state = PRODUCT

            await ask_product(
                chat_id,
                bot
            )
            return

        # اگر کاربر به‌جای دکمه، متن توضیح فرستاد
        order.cart[-1].details = text

        order.state = CART

        await show_cart(
            chat_id,
            bot
        )
        return

    # -----------------------------------------------------
    # CART
    # -----------------------------------------------------

    if order.state == CART:

        if normalized in [
            "➕ افزودن محصول",
            "افزودن محصول"
        ]:

            order.state = PRODUCT

            await ask_product(
                chat_id,
                bot
            )
            return

        if normalized in [
            "🛒 مشاهده سبد خرید",
            "مشاهده سبد خرید"
        ]:

            await show_cart(
                chat_id,
                bot
            )
            return

        if normalized in [
            "✅ ادامه ثبت سفارش",
            "ادامه ثبت سفارش"
        ]:

            if not order.cart:

                await bot.send_message(
                    chat_id=chat_id,
                    text="سبد خرید شما خالی است.",
                    reply_markup=cart_keyboard()
                )
                return

            order.state = ADDRESS

            await ask_address(
                chat_id,
                bot
            )
            return

        await bot.send_message(
            chat_id=chat_id,
            text="لطفاً یکی از گزینه‌های موجود را انتخاب کنید.",
            reply_markup=cart_keyboard()
        )
        return

    # -----------------------------------------------------
    # ADDRESS
    # -----------------------------------------------------

    if order.state == ADDRESS:

        if not text:

            await bot.send_message(
                chat_id=chat_id,
                text="لطفاً آدرس کامل را در یک پیام ارسال کنید.",
                reply_markup=cancel_keyboard()
            )
            return

        order.full_address = text
        order.state = SHIPPING

        await ask_shipping(
            chat_id,
            bot
        )
        return

    # -----------------------------------------------------
    # SHIPPING
    # -----------------------------------------------------

    if order.state == SHIPPING:

        if normalized in [
            "📦 پست",
            "پست"
        ]:

            order.shipping_method = SHIPPING_POST

        elif normalized in [
            "🏪 دریافت توسط مشتری",
            "دریافت توسط مشتری"
        ]:

            order.shipping_method = SHIPPING_PICKUP

        else:

            await bot.send_message(
                chat_id=chat_id,
                text="لطفاً یکی از روش‌های دریافت را انتخاب کنید.",
                reply_markup=shipping_keyboard()
            )
            return

        order.state = CUSTOMER_DESCRIPTION

        await ask_customer_description(
            chat_id,
            bot
        )
        return

    # -----------------------------------------------------
    # CUSTOMER DESCRIPTION
    # -----------------------------------------------------

    if order.state == CUSTOMER_DESCRIPTION:

        if normalized in [
            "⏭ بدون توضیح",
            "بدون توضیح"
        ]:

            order.customer_description = ""

        else:

            order.customer_description = text

        order.state = PRICE_STATUS

        await ask_price_status(
            chat_id,
            bot
        )
        return

    # -----------------------------------------------------
    # PRICE STATUS
    # -----------------------------------------------------

    if order.state == PRICE_STATUS:

        if normalized in [
            "💰 قیمت را می‌دانم",
            "قیمت را می‌دانم"
        ]:

            order.price_known = True
            order.state = PAYMENT

            await ask_price(
                chat_id,
                bot
            )
            return

        if normalized in [
            "❓ قیمت را نمی‌دانم",
            "قیمت را نمی‌دانم"
        ]:

            order.price_known = False

            try:

                await send_price_request_to_admin(
                    user_id,
                    order,
                    bot
                )

            except Exception:
                logger.exception(
                    "Could not send price request."
                )

                order.state = PRICE_STATUS
                order.waiting_for_admin_price = False

                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ ارسال درخواست قیمت با مشکل مواجه شد.\n"
                        "لطفاً دوباره تلاش کنید."
                    ),
                    reply_markup=price_keyboard()
                )
                return

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ اطلاعات سفارش شما برای فروشگاه ارسال شد.\n\n"
                    "💰 قیمت سفارش توسط فروشگاه بررسی می‌شود.\n"
                    "بعد از تعیین قیمت، مبلغ و اطلاعات پرداخت "
                    "برای شما ارسال خواهد شد.\n\n"
                    f"🆔 <b>شماره سفارش:</b>\n"
                    f"<code>{escape(order.order_id)}</code>"
                ),
                parse_mode="HTML",
                reply_markup=cancel_keyboard()
            )
            return

        await bot.send_message(
            chat_id=chat_id,
            text="لطفاً یکی از گزینه‌های قیمت را انتخاب کنید.",
            reply_markup=price_keyboard()
        )
        return

    # -----------------------------------------------------
    # PAYMENT
    # -----------------------------------------------------

    if order.state == PAYMENT:

        # اگر کاربر دکمه قدیمی را به‌صورت متن بفرستد
        if normalized in [
            "📸 ارسال رسید پرداخت",
            "ارسال رسید پرداخت"
        ]:

            await ask_receipt(
                chat_id,
                bot
            )
            return

        price = normalize_price(
            text
        )

        if not price:

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ مبلغ واردشده معتبر نیست.\n\n"
                    "مثال:\n"
                    "850000"
                ),
                reply_markup=cancel_keyboard()
            )
            return

        order.price = price
        order.state = RECEIPT

        await ask_payment(
            chat_id,
            bot,
            order
        )
        return

    # -----------------------------------------------------
    # RECEIPT
    # -----------------------------------------------------

    if order.state == RECEIPT:

        photo_file_id = get_photo_file_id(update)
        document_file_id = get_document_file_id(update)

        if photo_file_id:

            order.receipt_file_id = photo_file_id
            order.receipt_type = "photo"

        elif document_file_id:

            order.receipt_file_id = document_file_id
            order.receipt_type = "document"

        else:

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "📸 لطفاً تصویر رسید پرداخت را "
                    "به‌صورت عکس یا فایل ارسال کنید."
                ),
                reply_markup=cancel_keyboard()
            )
            return

        order.state = CONFIRM

        # دقیقاً یک پیام خلاصه سفارش برای مشتری
        await bot.send_message(
            chat_id=chat_id,
            text=customer_order_summary(order),
            parse_mode="HTML",
            reply_markup=confirm_keyboard()
        )
        return

    # -----------------------------------------------------
    # CONFIRM
    # -----------------------------------------------------

    if order.state == CONFIRM:

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "لطفاً اطلاعات سفارش را بررسی کنید و "
                "یکی از گزینه‌های زیر را انتخاب کنید."
            ),
            reply_markup=confirm_keyboard()
        )
        return

    # -----------------------------------------------------
    # WAITING ADMIN PRICE
    # -----------------------------------------------------

    if order.state == WAITING_ADMIN_PRICE:

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ سفارش شما در انتظار تعیین قیمت توسط فروشگاه است.\n\n"
                f"🆔 <b>شماره سفارش:</b>\n"
                f"<code>{escape(order.order_id)}</code>\n\n"
                "بعد از مشخص شدن قیمت، اطلاعات پرداخت "
                "برای شما ارسال می‌شود."
            ),
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
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
        "Unhandled bot error:",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if (
        not BOT_TOKEN
        or BOT_TOKEN == "توکن_ربات_اینجا"
    ):
        raise RuntimeError(
            "BOT_TOKEN را در ابتدای فایل تنظیم کنید."
        )

    if (
        not CARD_NUMBER
        or CARD_NUMBER == "شماره_کارت_اینجا"
    ):
        raise RuntimeError(
            "CARD_NUMBER را در ابتدای فایل تنظیم کنید."
        )

    if not isinstance(ADMIN_ID, int):
        raise RuntimeError(
            "ADMIN_ID باید عدد باشد."
        )

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command_handler
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # پیام‌های متنی، عکس و فایل مشتری
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            customer_message_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    print()
    print("=" * 50)
    print("Suryan Telegram Order Bot")
    print("Bot is running...")
    print(f"Admin ID: {ADMIN_ID}")
    print("=" * 50)
    print()

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# /start HANDLER
# =========================================================

async def start_command_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = get_user_id(update)

    if user_id is None:
        return

    reset_order(user_id)

    await send_welcome(
        update.effective_chat.id,
        context.bot
    )


if __name__ == "__main__":
    main()
