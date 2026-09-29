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
# فقط این 3 مورد را تنظیم کن
# =========================================================

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
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


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


SHIPPING_POST = "پست"
SHIPPING_PICKUP = "دریافت توسط مشتری"


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

# کلید:
# admin message_id
#
# مقدار:
# {
#     "user_id": ...,
#     "order_id": ...
# }
admin_price_requests: Dict[int, Dict] = {}


# =========================================================
# HELPERS
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

    cleaned = re.sub(
        r"[,\s٬،]",
        "",
        value
    )

    if not cleaned.isdigit():
        return None

    number = int(cleaned)

    if number <= 0:
        return None

    return f"{number:,}"


def get_user_id(update: Update) -> Optional[int]:
    if not update.effective_user:
        return None

    return update.effective_user.id


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


def cleanup_price_requests_for_user(
    user_id: int
):

    to_delete = []

    for message_id, data in admin_price_requests.items():

        if data.get("user_id") == user_id:
            to_delete.append(message_id)

    for message_id in to_delete:
        admin_price_requests.pop(
            message_id,
            None
        )


def reset_order(user_id: int):

    cleanup_price_requests_for_user(
        user_id
    )

    orders[user_id] = OrderData()

    return orders[user_id]


def get_photo_file_id(
    update: Update
) -> Optional[str]:

    if not update.message:
        return None

    if not update.message.photo:
        return None

    return update.message.photo[-1].file_id


def get_document_file_id(
    update: Update
) -> Optional[str]:

    if not update.message:
        return None

    if not update.message.document:
        return None

    return update.message.document.file_id


def find_order_by_order_id(
    order_id: str
):

    order_id = normalize_text(
        order_id
    ).upper()

    for user_id, order in orders.items():

        if order.order_id.upper() == order_id:
            return user_id, order

    return None, None


def extract_order_id(
    text: str
) -> Optional[str]:

    if not text:
        return None

    text = normalize_text(
        text
    ).upper()

    patterns = [
        r"شماره\s*سفارش\s*[:：]?\s*([A-Z0-9-]+)",
        r"ORDER\s*ID\s*[:：]?\s*([A-Z0-9-]+)",
        r"(S-[A-Z0-9-]+)",
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


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "🛍 شروع خرید"
                )
            ]
        ],
        resize_keyboard=True
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
# CART
# =========================================================

def build_cart_text(
    order: OrderData
) -> str:

    if not order.cart:
        return "🛒 <b>سبد خرید خالی است.</b>"

    lines = [
        "🛒 <b>محصولات سفارش:</b>"
    ]

    for index, item in enumerate(
        order.cart,
        start=1
    ):

        if item.product_type == "photo":

            title = (
                "📷 محصول ارسال‌شده به‌صورت عکس"
            )

        elif item.product_type == "document":

            title = (
                "📄 محصول ارسال‌شده به‌صورت فایل"
            )

        else:

            title = escape(
                item.product_text
            )

        lines.append(
            f"\n<b>محصول {index}</b>\n"
            f"{title}"
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

def build_customer_summary(
    order: OrderData
) -> str:

    price = (
        f"{escape(order.price)} تومان"
        if order.price
        else "تعیین نشده"
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

        f"📝 <b>توضیحات:</b>\n"
        f"{escape(order.customer_description or 'ندارد')}\n\n"

        f"💰 <b>مبلغ:</b> "
        f"{price}\n\n"

        "لطفاً اطلاعات را بررسی کنید."
    )


# =========================================================
# ADMIN SUMMARY
# =========================================================

def build_admin_summary(
    user_id: int,
    order: OrderData
) -> str:

    price = (
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
        f"{price}"
    )


# =========================================================
# QUESTIONS
# =========================================================

async def ask_first_name(
    update: Update
):

    await update.message.reply_text(
        "👤 لطفاً <b>نام</b> خود را وارد کنید.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )


async def ask_last_name(
    update: Update
):

    await update.message.reply_text(
        "👤 لطفاً <b>نام خانوادگی</b> خود را وارد کنید.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )


async def ask_phone(
    update: Update
):

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
        one_time_keyboard=True
    )

    await update.message.reply_text(
        "📱 لطفاً شماره تماس خود را ارسال کنید.\n\n"
        "می‌توانید از دکمه زیر استفاده کنید.",
        reply_markup=keyboard
    )


async def ask_product(
    update: Update
):

    await update.message.reply_text(
        "🛍 لطفاً محصول را ارسال کنید.\n\n"
        "می‌توانید عکس، فایل یا کد/نام محصول را بفرستید.",
        reply_markup=product_keyboard()
    )


async def ask_product_details(
    update: Update
):

    await update.message.reply_text(
        "📝 اگر توضیحی درباره محصول دارید، "
        "مثل رنگ، سایز یا تعداد، ارسال کنید.\n\n"
        "اگر توضیحی ندارید، روی «بدون توضیحات» بزنید.",
        reply_markup=product_details_keyboard()
    )


async def ask_address(
    update: Update
):

    await update.message.reply_text(
        "📍 لطفاً <b>آدرس کامل</b> خود را وارد کنید.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )


async def ask_shipping(
    update: Update
):

    await update.message.reply_text(
        "🚚 روش دریافت سفارش را انتخاب کنید:",
        reply_markup=shipping_keyboard()
    )


async def ask_description(
    update: Update
):

    await update.message.reply_text(
        "📝 اگر توضیح دیگری درباره سفارش دارید، "
        "بنویسید.\n\n"
        "اگر توضیحی ندارید، «ندارم» بفرستید."
    )


async def ask_price_status(
    update: Update
):

    await update.message.reply_text(
        "💰 آیا قیمت محصول را می‌دانید؟",
        reply_markup=price_keyboard()
    )


async def ask_price(
    update: Update
):

    await update.message.reply_text(
        "💰 مبلغ نهایی سفارش را به تومان وارد کنید.\n\n"
        "مثال:\n"
        "<code>850000</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )


async def ask_receipt(
    update: Update
):

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

    await update.message.reply_text(
        "سلام 👋\n\n"
        "به فروشگاه سورین خوش آمدید. 🛍\n\n"
        "برای شروع خرید روی دکمه زیر بزنید.",
        reply_markup=main_keyboard()
    )


# =========================================================
# PRICE REQUEST
# =========================================================

async def send_price_request_to_admin(
    user_id: int,
    order: OrderData,
    bot
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

        "⬅️ برای تعیین قیمت، روی همین پیام "
        "<b>Reply</b> کنید و فقط مبلغ را بفرستید.\n\n"

        "مثال:\n"
        "<code>850000</code>"
    )

    message = await bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
        parse_mode="HTML"
    )

    order.admin_price_message_id = (
        message.message_id
    )

    order.waiting_for_admin_price = True
    order.state = WAITING_ADMIN_PRICE

    admin_price_requests[
        message.message_id
    ] = {
        "user_id": user_id,
        "order_id": order.order_id,
    }


# =========================================================
# DELIVER PRICE
# =========================================================

async def deliver_admin_price(
    user_id: int,
    order: OrderData,
    price: str,
    admin_message_id: Optional[int],
    bot
):

    try:

        await bot.send_message(
            chat_id=user_id,
            text=(
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

    except TelegramError:

        logger.exception(
            "Could not send price to customer."
        )

        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "❌ ارسال قیمت به مشتری انجام نشد.\n\n"
                f"شماره سفارش: "
                f"<code>{escape(order.order_id)}</code>"
            ),
            parse_mode="HTML"
        )

        return False

    order.price = price
    order.price_known = True
    order.waiting_for_admin_price = False
    order.state = RECEIPT

    cleanup_price_request(
        admin_message_id
    )

    cleanup_price_request(
        order.admin_price_message_id
    )

    order.admin_price_message_id = None

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "✅ قیمت برای مشتری ارسال شد.\n\n"
            f"🔖 سفارش: "
            f"<code>{escape(order.order_id)}</code>\n"
            f"💰 مبلغ: <b>{escape(price)} تومان</b>"
        ),
        parse_mode="HTML"
    )

    return True


# =========================================================
# FINAL ORDER TO ADMIN
# =========================================================

async def send_final_order_to_admin(
    user_id: int,
    order: OrderData,
    bot
):

    # خلاصه سفارش
    try:

        await bot.send_message(
            chat_id=ADMIN_ID,
            text=build_admin_summary(
                user_id,
                order
            ),
            parse_mode="HTML"
        )

    except Exception:

        logger.exception(
            "Could not send order summary."
        )

        return False

    # محصولات
    for index, item in enumerate(
        order.cart,
        start=1
    ):

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

                await bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=item.product_file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif item.product_type == "document":

                await bot.send_document(
                    chat_id=ADMIN_ID,
                    document=item.product_file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

        except Exception:

            logger.exception(
                "Could not send product media."
            )

    # رسید
    if order.receipt_file_id:

        try:

            caption = (
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

                await bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=order.receipt_file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif order.receipt_type == "document":

                await bot.send_document(
                    chat_id=ADMIN_ID,
                    document=order.receipt_file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

        except Exception:

            logger.exception(
                "Could not send receipt."
            )

            return False

    # پیام نهایی
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "✅ <b>سفارش کامل دریافت شد.</b>\n\n"
            f"🔖 شماره سفارش: "
            f"<code>{escape(order.order_id)}</code>\n\n"
            "📦 اطلاعات سفارش، محصولات و رسید ارسال شدند."
        ),
        parse_mode="HTML"
    )

    return True


# =========================================================
# CALLBACKS
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

    data = query.data

    # لغو
    if data == "cancel_order":

        reset_order(user_id)

        await query.edit_message_text(
            "❌ سفارش لغو شد."
        )

        await query.message.reply_text(
            "🛍 برای شروع دوباره:",
            reply_markup=main_keyboard()
        )

        return

    # شروع خرید
    if data == "start_shopping":

        reset_order(user_id)

        order = get_order(user_id)

        order.state = NAME

        await query.edit_message_text(
            "🛍 خرید جدید شروع شد."
        )

        await ask_first_name(
            update
        )

        return

    # افزودن محصول
    if data == "add_product":

        order.state = PRODUCT

        await query.edit_message_text(
            "➕ افزودن محصول جدید"
        )

        await ask_product(
            update
        )

        return

    # ادامه سفارش
    if data == "continue_order":

        if not order.cart:

            await query.answer(
                "ابتدا محصول اضافه کنید.",
                show_alert=True
            )

            return

        order.state = ADDRESS

        await query.edit_message_text(
            "➡️ ادامه سفارش"
        )

        await ask_address(
            update
        )

        return

    # بدون توضیحات
    if data == "skip_product_details":

        order.state = CART

        await query.edit_message_text(
            "✅ توضیحات محصول ثبت نشد."
        )

        await query.message.reply_text(
            build_cart_text(order),
            parse_mode="HTML",
            reply_markup=cart_keyboard()
        )

        return

    # پست
    if data == "shipping_post":

        order.shipping_method = SHIPPING_POST
        order.state = CUSTOMER_DESCRIPTION

        await query.edit_message_text(
            "📦 روش دریافت: پست"
        )

        await ask_description(
            update
        )

        return

    # دریافت حضوری
    if data == "shipping_pickup":

        order.shipping_method = SHIPPING_PICKUP
        order.state = CUSTOMER_DESCRIPTION

        await query.edit_message_text(
            "🏪 روش دریافت: دریافت توسط مشتری"
        )

        await ask_description(
            update
        )

        return

    # قیمت معلوم
    if data == "price_known":

        order.price_known = True
        order.state = PAYMENT

        await query.edit_message_text(
            "💰 قیمت مشخص است."
        )

        await ask_price(
            update
        )

        return

    # قیمت نامعلوم
    if data == "price_unknown":

        order.price_known = False

        await query.edit_message_text(
            "⏳ درخواست قیمت برای مدیریت ارسال شد."
        )

        try:

            await send_price_request_to_admin(
                user_id,
                order,
                context.bot
            )

            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "⏳ <b>درخواست قیمت ارسال شد.</b>\n\n"
                    f"🔖 شماره سفارش: "
                    f"<code>{escape(order.order_id)}</code>\n\n"
                    "پس از تعیین قیمت، مبلغ برای شما ارسال می‌شود."
                ),
                parse_mode="HTML"
            )

        except Exception:

            logger.exception(
                "Could not send price request."
            )

            order.state = PRICE_STATUS
            order.waiting_for_admin_price = False

            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "❌ ارسال درخواست قیمت با مشکل مواجه شد.\n"
                    "لطفاً دوباره تلاش کنید."
                ),
                reply_markup=price_keyboard()
            )

        return

    # ارسال رسید
    if data == "send_receipt":

        order.state = RECEIPT

        await query.edit_message_text(
            "📤 لطفاً رسید پرداخت را به‌صورت عکس یا فایل ارسال کنید."
        )

        return

    # تأیید نهایی
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
            context.bot
        )

        if not success:

            await query.answer(
                "ارسال سفارش با مشکل مواجه شد.",
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

    # ویرایش
    if data == "edit_order":

        reset_order(user_id)

        order = get_order(user_id)

        order.state = NAME

        await query.edit_message_text(
            "✏️ ویرایش سفارش شروع شد."
        )

        await ask_first_name(
            update
        )

        return


# =========================================================
# ADMIN
# =========================================================

async def handle_admin_message(
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

    # -----------------------------------------------------
    # روش قدیمی /price
    # -----------------------------------------------------

    if text.lower().startswith(
        "/price"
    ):

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

            customer_id = int(
                parts[1]
            )

        except ValueError:

            await update.message.reply_text(
                "❌ USER_ID باید عدد باشد."
            )

            return

        price = normalize_price(
            parts[2]
        )

        if price is None:

            await update.message.reply_text(
                "❌ مبلغ نامعتبر است."
            )

            return

        order = orders.get(
            customer_id
        )

        if not order:

            await update.message.reply_text(
                "❌ سفارش پیدا نشد."
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
            order.admin_price_message_id,
            context.bot
        )

        return

    # -----------------------------------------------------
    # Reply به درخواست قیمت
    # -----------------------------------------------------

    replied = (
        update.message.reply_to_message
    )

    if replied:

        replied_id = replied.message_id

        request = admin_price_requests.get(
            replied_id
        )

        user_id = None
        order = None

        # مسیر اصلی
        if request:

            user_id = request.get(
                "user_id"
            )

            order_id = request.get(
                "order_id"
            )

            candidate = orders.get(
                user_id
            )

            if (
                candidate
                and candidate.order_id == order_id
            ):

                order = candidate

        # مسیر پشتیبان
        if order is None:

            replied_text = (
                replied.text
                or replied.caption
                or ""
            )

            order_id = extract_order_id(
                replied_text
            )

            if order_id:

                user_id, order = (
                    find_order_by_order_id(
                        order_id
                    )
                )

        if order is None:

            await update.message.reply_text(
                "❌ سفارش مربوط به این پیام پیدا نشد.\n\n"
                "مستقیماً روی پیام درخواست قیمت Reply کنید."
            )

            return

        if not order.waiting_for_admin_price:

            await update.message.reply_text(
                "⚠️ این سفارش دیگر در انتظار قیمت نیست."
            )

            return

        price = normalize_price(
            text
        )

        if price is None:

            await update.message.reply_text(
                "❌ مبلغ معتبر نیست.\n\n"
                "مثال:\n"
                "850000"
            )

            return

        await deliver_admin_price(
            user_id,
            order,
            price,
            replied_id,
            context.bot
        )

        return

    # -----------------------------------------------------
    # پیام عادی ادمین
    # -----------------------------------------------------

    await update.message.reply_text(
        "برای تعیین قیمت، روی پیام «درخواست قیمت» Reply کنید "
        "و فقط مبلغ را بفرستید.\n\n"
        "مثال:\n"
        "850000"
    )


# =========================================================
# CUSTOMER
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

    # اگر ادمین است
    if user_id == ADMIN_ID:

        await handle_admin_message(
            update,
            context
        )

        return

    order = get_order(
        user_id
    )

    text = get_text(
        update
    )

    # =====================================================
    # START
    # =====================================================

    if order.state == START:

        if text == "🛍 شروع خرید":

            reset_order(
                user_id
            )

            order = get_order(
                user_id
            )

            order.state = NAME

            await ask_first_name(
                update
            )

            return

        await update.message.reply_text(
            "برای شروع خرید روی دکمه زیر بزنید.",
            reply_markup=main_keyboard()
        )

        return

    # =====================================================
    # NAME
    # =====================================================

    if order.state == NAME:

        if not text:

            await update.message.reply_text(
                "❌ لطفاً نام را وارد کنید."
            )

            return

        order.first_name = text
        order.state = LAST_NAME

        await ask_last_name(
            update
        )

        return

    # =====================================================
    # LAST NAME
    # =====================================================

    if order.state == LAST_NAME:

        if not text:

            await update.message.reply_text(
                "❌ لطفاً نام خانوادگی را وارد کنید."
            )

            return

        order.last_name = text
        order.state = PHONE

        await ask_phone(
            update
        )

        return

    # =====================================================
    # PHONE
    # =====================================================

    if order.state == PHONE:

        if update.message.contact:

            phone = (
                update.message.contact.phone_number
            )

        else:

            phone = text

        phone = normalize_digits(
            phone
        )

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
                "09121234567"
            )

            return

        order.phone = phone
        order.state = PRODUCT

        await update.message.reply_text(
            "✅ شماره تماس ثبت شد.",
            reply_markup=ReplyKeyboardRemove()
        )

        await ask_product(
            update
        )

        return

    # =====================================================
    # PRODUCT
    # =====================================================

    if order.state == PRODUCT:

        photo = get_photo_file_id(
            update
        )

        document = get_document_file_id(
            update
        )

        if photo:

            item = CartItem(
                product_type="photo",
                product_file_id=photo
            )

        elif document:

            item = CartItem(
                product_type="document",
                product_file_id=document
            )

        elif text:

            item = CartItem(
                product_type="text",
                product_text=text
            )

        else:

            await update.message.reply_text(
                "❌ لطفاً عکس، فایل یا نام/کد محصول را بفرستید."
            )

            return

        order.cart.append(
            item
        )

        order.state = PRODUCT_DETAILS

        await update.message.reply_text(
            "✅ محصول اضافه شد."
        )

        await ask_product_details(
            update
        )

        return

    # =====================================================
    # PRODUCT DETAILS
    # =====================================================

    if order.state == PRODUCT_DETAILS:

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

    # =====================================================
    # CART
    # =====================================================

    if order.state == CART:

        await update.message.reply_text(
            "لطفاً یکی از گزینه‌های زیر را انتخاب کنید.",
            reply_markup=cart_keyboard()
        )

        return

    # =====================================================
    # ADDRESS
    # =====================================================

    if order.state == ADDRESS:

        if len(text) < 10:

            await update.message.reply_text(
                "❌ لطفاً آدرس کامل‌تری وارد کنید."
            )

            return

        order.full_address = text
        order.state = SHIPPING

        await ask_shipping(
            update
        )

        return

    # =====================================================
    # DESCRIPTION
    # =====================================================

    if order.state == CUSTOMER_DESCRIPTION:

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

        await ask_price_status(
            update
        )

        return

    # =====================================================
    # PRICE STATUS
    # =====================================================

    if order.state == PRICE_STATUS:

        await update.message.reply_text(
            "لطفاً یکی از گزینه‌های قیمت را انتخاب کنید.",
            reply_markup=price_keyboard()
        )

        return

    # =====================================================
    # PAYMENT
    # =====================================================

    if order.state == PAYMENT:

        price = normalize_price(
            text
        )

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

        await ask_receipt(
            update
        )

        return

    # =====================================================
    # WAITING PRICE
    # =====================================================

    if order.state == WAITING_ADMIN_PRICE:

        await update.message.reply_text(
            "⏳ هنوز قیمت سفارش توسط مدیریت مشخص نشده است."
        )

        return

    # =====================================================
    # RECEIPT
    # =====================================================

    if order.state == RECEIPT:

        photo = get_photo_file_id(
            update
        )

        document = get_document_file_id(
            update
        )

        if photo:

            order.receipt_file_id = photo
            order.receipt_type = "photo"

        elif document:

            order.receipt_file_id = document
            order.receipt_type = "document"

        else:

            await update.message.reply_text(
                "❌ لطفاً رسید را به‌صورت عکس یا فایل ارسال کنید."
            )

            return

        order.state = CONFIRM

        await update.message.reply_text(
            "✅ رسید پرداخت دریافت شد.\n\n"
            + build_customer_summary(order),
            parse_mode="HTML",
            reply_markup=confirm_keyboard()
        )

        return

    # =====================================================
    # CONFIRM
    # =====================================================

    if order.state == CONFIRM:

        await update.message.reply_text(
            "لطفاً از دکمه‌های زیر استفاده کنید.",
            reply_markup=confirm_keyboard()
        )

        return


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "Bot error:",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if (
        not TELEGRAM_BOT_TOKEN
        or TELEGRAM_BOT_TOKEN == "توکن_ربات_اینجا"
    ):

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN را در ابتدای فایل تنظیم کنید."
        )

    if (
        not CARD_NUMBER
        or CARD_NUMBER == "شماره_کارت_اینجا"
    ):

        raise RuntimeError(
            "CARD_NUMBER را در ابتدای فایل تنظیم کنید."
        )

    if not isinstance(
        ADMIN_ID,
        int
    ):

        raise RuntimeError(
            "ADMIN_ID باید عدد باشد."
        )

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

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
    print("=" * 45)
    print("Suryan Telegram Bot")
    print("Bot is running...")
    print(f"Admin ID: {ADMIN_ID}")
    print("=" * 45)
    print()

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
