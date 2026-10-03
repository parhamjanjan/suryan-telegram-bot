import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# ENV
# =========================================================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CARD_NUMBER = os.getenv(
    "CARD_NUMBER",
    "شماره کارت در تنظیمات ربات وارد نشده است",
)

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN در فایل .env تنظیم نشده است"
    )

# =========================================================
# CONFIG
# =========================================================

# شناسه عددی ادمین
ADMIN_ID = 6285612246

SHIPPING_POST = "پست"
SHIPPING_PICKUP = "دریافت توسط مشتری"

# هزینه‌های ثابت
POST_FEE = 200_000
PICKUP_PACKAGING_FEE = 45_000

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

    # مبلغ نهایی شامل هزینه ارسال/بسته‌بندی
    price: str = ""

    # رسید
    receipt_file_id: Optional[str] = None
    receipt_type: str = ""

    waiting_for_admin_price: bool = False

    # پیام درخواست قیمت ارسال‌شده برای ادمین
    admin_price_message_id: Optional[int] = None


# user_id -> OrderData
orders: Dict[int, OrderData] = {}

# admin_message_id -> {"user_id": ..., "order_id": ...}
admin_price_requests: Dict[int, Dict[str, Any]] = {}


# =========================================================
# TEXT HELPERS
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
    if not value:
        return ""

    translation = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )

    return str(value).translate(translation)


def normalize_price(value: str) -> Optional[str]:
    if not value:
        return None

    value = normalize_digits(value)

    value = (
        value
        .replace(",", "")
        .replace("٬", "")
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


def get_shipping_fee(order: OrderData) -> int:
    if order.shipping_method == SHIPPING_POST:
        return POST_FEE

    if order.shipping_method == SHIPPING_PICKUP:
        return PICKUP_PACKAGING_FEE

    return 0


def add_shipping_fee(order: OrderData, base_price: str) -> str:
    normalized = normalize_price(base_price)

    if not normalized:
        raise ValueError("قیمت پایه معتبر نیست.")

    base_number = int(
        normalized.replace(",", "").replace(" تومان", "")
    )

    total = base_number + get_shipping_fee(order)

    return f"{total:,} تومان"


def format_shipping_fee(order: OrderData) -> str:
    return f"{get_shipping_fee(order):,} تومان"


# =========================================================
# TELEGRAM MESSAGE HELPERS
# =========================================================


def get_message_id(message) -> Optional[int]:
    if message is None:
        return None

    return getattr(message, "message_id", None)


def get_replied_message(message):
    if message is None:
        return None

    return getattr(message, "reply_to_message", None)


def get_replied_message_id(message) -> Optional[int]:
    replied = get_replied_message(message)

    if replied is None:
        return None

    return get_message_id(replied)


def get_message_text(message) -> str:
    if message is None:
        return ""

    return (
        getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    )


def extract_order_id(text: str) -> Optional[str]:
    if not text:
        return None

    pattern = (
        r"شماره\s*سفارش\s*[:：]?\s*"
        r"([A-Z0-9\-]+)"
    )

    match = re.search(
        pattern,
        text,
        re.IGNORECASE,
    )

    if match:
        return match.group(1).upper()

    return None


# =========================================================
# FILE HELPERS
# =========================================================


def get_photo_file_id(message) -> Optional[str]:
    if not message or not message.photo:
        return None

    try:
        # آخرین سایز عکس معمولاً بزرگ‌ترین نسخه است.
        return message.photo[-1].file_id
    except Exception:
        return None


def get_document_file_id(message) -> Optional[str]:
    if not message or not message.document:
        return None

    try:
        return message.document.file_id
    except Exception:
        return None


# =========================================================
# ORDER MANAGEMENT
# =========================================================


def get_order(user_id: int) -> OrderData:
    if user_id not in orders:
        orders[user_id] = OrderData()

    return orders[user_id]


def cleanup_price_request(
    user_id: int,
    order_id: Optional[str] = None,
):
    keys_to_delete = []

    for message_id, data in list(admin_price_requests.items()):
        if data.get("user_id") != user_id:
            continue

        if order_id is not None:
            if data.get("order_id") != order_id:
                continue

        keys_to_delete.append(message_id)

    for key in keys_to_delete:
        admin_price_requests.pop(key, None)


def reset_order(user_id: int):
    old_order = orders.get(user_id)

    if old_order:
        cleanup_price_request(
            user_id,
            old_order.order_id,
        )

    orders[user_id] = OrderData()


def find_order_by_order_id(order_id: str):
    if not order_id:
        return None, None

    order_id = order_id.upper()

    for user_id, order in orders.items():
        if order.order_id.upper() == order_id:
            return user_id, order

    return None, None


# =========================================================
# INLINE KEYBOARDS
# =========================================================
#
# تمام دکمه‌ها Inline هستند؛ بنابراین دقیقاً زیر همان پیامی
# که با reply_markup ارسال شده‌اند نمایش داده می‌شوند.
# =========================================================


def inline_keyboard(rows):
    return InlineKeyboardMarkup(rows)


def main_start_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "🛍 شروع خرید",
                callback_data="start_purchase",
            )
        ]
    ])


def cancel_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order",
            )
        ]
    ])


def product_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "➕ افزودن محصول",
                callback_data="add_product",
            )
        ],
        [
            InlineKeyboardButton(
                "🛒 مشاهده سبد خرید",
                callback_data="show_cart",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order",
            )
        ],
    ])


def cart_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "➕ افزودن محصول",
                callback_data="add_product",
            )
        ],
        [
            InlineKeyboardButton(
                "✅ ادامه ثبت سفارش",
                callback_data="continue_order",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order",
            )
        ],
    ])


def product_details_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "⏭ بدون توضیح",
                callback_data="no_product_details",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order",
            )
        ],
    ])


def shipping_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "📦 پست",
                callback_data="shipping_post",
            )
        ],
        [
            InlineKeyboardButton(
                "🏪 دریافت توسط مشتری",
                callback_data="shipping_pickup",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order",
            )
        ],
    ])


def customer_description_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "⏭ بدون توضیح",
                callback_data="no_customer_description",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order",
            )
        ],
    ])


def price_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "💰 قیمت را می‌دانم",
                callback_data="price_known",
            )
        ],
        [
            InlineKeyboardButton(
                "❓ قیمت را نمی‌دانم",
                callback_data="price_unknown",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order",
            )
        ],
    ])


def payment_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "📸 ارسال رسید پرداخت",
                callback_data="send_receipt",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order",
            )
        ],
    ])


def confirm_keyboard():
    return inline_keyboard([
        [
            InlineKeyboardButton(
                "✅ تأیید نهایی سفارش",
                callback_data="confirm_order",
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ ویرایش سفارش",
                callback_data="edit_order",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سفارش",
                callback_data="cancel_order",
            )
        ],
    ])


# =========================================================
# WELCOME / ASK FUNCTIONS
# =========================================================


async def send_welcome(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user:
        return

    user_id = user.id

    reset_order(user_id)

    await update.effective_message.reply_text(
        "🛍 به فروشگاه سورین خوش آمدید!\n\n"
        "برای ثبت سفارش، روی دکمه «🛍 شروع خرید» بزنید.",
        reply_markup=main_start_keyboard(),
    )


async def start_purchase(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user:
        return

    user_id = user.id

    reset_order(user_id)

    order = get_order(user_id)
    order.state = NAME

    await update.effective_message.reply_text(
        "عالیه 🌱\n\n"
        "برای شروع ثبت سفارش، لطفاً نام و نام خانوادگی "
        "خود را وارد کنید.",
        reply_markup=cancel_keyboard(),
    )


async def ask_phone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "📱 لطفاً شماره تماس خود را به‌صورت متنی ارسال کنید.",
        reply_markup=cancel_keyboard(),
    )


async def ask_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "🛍 حالا محصول موردنظر را ارسال کنید.\n\n"
        "می‌توانید یکی از این موارد را بفرستید:\n"
        "• عکس محصول\n"
        "• کد محصول\n"
        "• نام محصول\n"
        "• لینک محصول\n\n"
        "بعد از ارسال محصول، اطلاعات تکمیلی آن را "
        "از شما می‌پرسم.",
        reply_markup=product_keyboard(),
    )


async def ask_product_details(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "📝 اگر برای این محصول توضیحی دارید، همینجا بنویسید.\n\n"
        "مثلاً:\n"
        "سایز L، رنگ مشکی\n\n"
        "اگر توضیحی ندارید، روی «⏭ بدون توضیح» بزنید.",
        reply_markup=product_details_keyboard(),
    )


async def ask_address(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "📍 لطفاً آدرس کامل را در یک پیام ارسال کنید.\n\n"
        "ساختار پیشنهادی:\n\n"
        "استان: ...\n"
        "شهر: ...\n"
        "آدرس دقیق: ...\n"
        "کدپستی: ...\n\n"
        "مثال:\n"
        "استان: البرز\n"
        "شهر: کرج\n"
        "آدرس دقیق: مهرشهر، بلوار ارم، خیابان ...، "
        "پلاک ۱۲، واحد ۳\n"
        "کدپستی: ۳۱۸۷۶۴۵۱۲۳\n\n"
        "⚠️ اگر کدپستی را دارید، آن را در آخر آدرس بنویسید.\n"
        "اگر کدپستی ندارید، فقط آدرس کامل را ارسال کنید.",
        reply_markup=cancel_keyboard(),
    )


async def ask_shipping(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "🚚 روش دریافت سفارش را انتخاب کنید:",
        reply_markup=shipping_keyboard(),
    )


async def ask_customer_description(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "📝 اگر توضیح یا درخواست خاصی برای سفارش دارید، بنویسید.\n\n"
        "مثلاً:\n"
        "لطفاً قبل از ارسال با من تماس بگیرید.\n\n"
        "اگر توضیحی ندارید، روی «⏭ بدون توضیح» بزنید.",
        reply_markup=customer_description_keyboard(),
    )


async def ask_price_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "💰 آیا قیمت محصول و هزینه ارسال و موجودی را می‌دانید؟ "
        "و با ادمین هماهنگ کردید؟",
        reply_markup=price_keyboard(),
    )


async def ask_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user:
        return

    order = get_order(user.id)

    await update.effective_message.reply_text(
        "💳 مبلغ نهایی سفارش شما (با احتساب هزینه دریافت):\n\n"
        f"💰 {order.price}\n\n"
        "لطفاً مبلغ بالا را به شماره کارت زیر واریز کنید:\n\n"
        f"💳 {CARD_NUMBER}\n\n"
        "بعد از پرداخت، تصویر رسید پرداخت را ارسال کنید.",
        reply_markup=payment_keyboard(),
    )


# =========================================================
# CART
# =========================================================


def cart_text(order: OrderData) -> str:
    if not order.cart:
        return "🛒 سبد خرید شما خالی است."

    lines = [
        "🛒 سبد خرید شما:",
        "",
    ]

    for index, item in enumerate(order.cart, start=1):
        if item.product_type == "photo":
            product_name = "📷 محصول تصویری"
        elif item.product_type == "document":
            product_name = "📄 محصول ارسالی"
        else:
            product_name = item.product_text or "محصول"

        lines.append(f"{index}. {product_name}")

        if item.details:
            lines.append(f"   📝 {item.details}")

        lines.append("")

    return "\n".join(lines)


async def show_cart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user:
        return

    order = get_order(user.id)

    await update.effective_message.reply_text(
        cart_text(order),
        reply_markup=cart_keyboard(),
    )


# =========================================================
# CUSTOMER SUMMARY
# =========================================================


def customer_order_summary(order: OrderData) -> str:
    customer_name = (
        f"{order.first_name} {order.last_name}"
    ).strip()

    return (
        "🧾 اطلاعات کامل سفارش شما\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🆔 شماره سفارش:\n"
        f"{order.order_id}\n\n"
        "👤 اطلاعات مشتری\n"
        f"نام: {customer_name}\n"
        f"📱 شماره تماس: {order.phone}\n\n"
        "🛒 محصولات\n"
        f"{cart_text(order)}\n"
        "📍 آدرس\n"
        f"{order.full_address}\n\n"
        "🚚 روش دریافت\n"
        f"{order.shipping_method}\n"
        f"💸 هزینه ارسال/بسته‌بندی: {get_shipping_fee(order):,} تومان\n\n"
        "💰 مبلغ نهایی سفارش\n"
        f"{order.price}\n\n"
        "📝 توضیحات\n"
        f"{order.customer_description or 'ندارد'}\n\n"
        "💳 وضعیت پرداخت\n"
        "رسید پرداخت دریافت شد\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ لطفاً تمام اطلاعات بالا را بررسی کنید.\n"
        "اگر همه اطلاعات صحیح است، روی «✅ تأیید نهایی سفارش» بزنید.\n\n"
        "در صورت وجود اشتباه، «✏️ ویرایش سفارش» را انتخاب کنید."
    )


# =========================================================
# SEND PRICE REQUEST TO ADMIN
# =========================================================


async def send_price_request_to_admin(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    order: OrderData,
):
    customer_name = (
        f"{order.first_name} {order.last_name}"
    ).strip()

    text = (
        "💰 درخواست تعیین قیمت جدید\n\n"
        f"🆔 شماره سفارش: {order.order_id}\n"
        f"👤 مشتری: {customer_name}\n"
        f"📱 شماره تماس: {order.phone}\n\n"
        f"🛒 تعداد محصولات: {len(order.cart)}\n\n"
        "📦 سبد خرید:\n"
        f"{cart_text(order)}\n\n"
        "📍 آدرس:\n"
        f"{order.full_address}\n\n"
        f"🚚 روش ارسال: {order.shipping_method}\n"
        f"💸 هزینه دریافت: {get_shipping_fee(order):,} تومان\n\n"
        "📝 توضیحات مشتری:\n"
        f"{order.customer_description or 'ندارد'}\n\n"
        "━━━━━━━━━━━━━━\n"
        "⚠️ برای تعیین قیمت، روی همین پیام Reply کنید.\n\n"
        "فقط قیمت خودِ محصولات را ارسال کنید؛ "
        "هزینه دریافت خودکار اضافه می‌شود:\n"
        "850000\n\n"
        "یا:\n"
        "/price 850000\n"
        "━━━━━━━━━━━━━━"
    )

    admin_message = await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
    )

    admin_message_id = admin_message.message_id

    admin_price_requests[admin_message_id] = {
        "user_id": user_id,
        "order_id": order.order_id,
    }

    order.admin_price_message_id = admin_message_id
    order.waiting_for_admin_price = True
    order.state = WAITING_ADMIN_PRICE

    print(
        "PRICE REQUEST SENT:",
        admin_message_id,
        order.order_id,
        user_id,
    )

    # ارسال فایل‌های محصولات بعد از پیام اصلی
    for item in order.cart:
        if (
            item.product_type == "photo"
            and item.product_file_id
        ):
            try:
                await context.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=item.product_file_id,
                    caption=(
                        "📷 محصول سفارش\n"
                        f"🆔 {order.order_id}"
                    ),
                )
            except Exception as e:
                print(
                    "SEND PRODUCT PHOTO ERROR:",
                    repr(e),
                )

        elif (
            item.product_type == "document"
            and item.product_file_id
        ):
            try:
                await context.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=item.product_file_id,
                    caption=(
                        "📄 محصول سفارش\n"
                        f"🆔 {order.order_id}"
                    ),
                )
            except Exception as e:
                print(
                    "SEND PRODUCT DOCUMENT ERROR:",
                    repr(e),
                )


# =========================================================
# ADMIN PRICE PROCESSOR
# =========================================================


async def process_admin_price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    if user.id != ADMIN_ID:
        return

    text = normalize_text(get_message_text(message))

    if not text:
        return

    customer_id = None
    order = None
    order_id = None

    # -----------------------------------------------------
    # اولویت اول: Reply به همان پیام درخواست قیمت
    # -----------------------------------------------------

    replied_message_id = get_replied_message_id(message)

    print(
        "ADMIN MESSAGE:",
        repr(text),
        "REPLIED MESSAGE ID:",
        replied_message_id,
    )

    if replied_message_id is not None:
        request = admin_price_requests.get(replied_message_id)

        if request:
            customer_id = request.get("user_id")
            order_id = request.get("order_id")

            current_order = orders.get(customer_id)

            if (
                current_order
                and current_order.order_id == order_id
            ):
                order = current_order

        # fallback با شماره سفارش موجود در پیام Reply شده
        if order is None:
            replied_message = get_replied_message(message)

            replied_text = get_message_text(replied_message)

            extracted_order_id = extract_order_id(replied_text)

            if extracted_order_id:
                (
                    customer_id,
                    order,
                ) = find_order_by_order_id(
                    extracted_order_id
                )

                order_id = extracted_order_id

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
            except Exception:
                await message.reply_text(
                    "❌ شناسه کاربر معتبر نیست."
                )
                return

            legacy_order = orders.get(legacy_user_id)

            if legacy_order is None:
                await message.reply_text(
                    "❌ سفارش فعالی برای این کاربر پیدا نشد."
                )
                return

            customer_id = legacy_user_id
            order = legacy_order
            order_id = legacy_order.order_id

    # -----------------------------------------------------
    # سفارش پیدا نشد
    # -----------------------------------------------------

    if order is None or customer_id is None:
        # پیام‌های عادی ادمین را دست نزن
        if (
            text.lower().startswith("/price")
            or replied_message_id is not None
        ):
            await message.reply_text(
                "❌ سفارش مربوط به این پیام پیدا نشد.\n\n"
                "برای جلوگیری از اشتباه، روی همان پیام "
                "«💰 درخواست تعیین قیمت» Reply کنید."
            )
        return

    # -----------------------------------------------------
    # وضعیت سفارش
    # -----------------------------------------------------

    if order.state != WAITING_ADMIN_PRICE:
        await message.reply_text(
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
        await message.reply_text(
            "❌ قیمت معتبر نیست.\n\n"
            "مثال:\n"
            "850000"
        )
        return

    # -----------------------------------------------------
    # اضافه کردن هزینه ارسال/بسته‌بندی
    # -----------------------------------------------------

    try:
        order.price = add_shipping_fee(order, price)
    except ValueError:
        await message.reply_text(
            "❌ قیمت معتبر نیست.\n\n"
            "مثال:\n"
            "850000"
        )
        return

    order.price_known = True
    order.waiting_for_admin_price = False
    order.state = RECEIPT

    cleanup_price_request(
        customer_id,
        order.order_id,
    )

    print(
        "PRICE SAVED:",
        order.order_id,
        order.price,
    )

    # -----------------------------------------------------
    # ارسال قیمت به مشتری
    # -----------------------------------------------------

    try:
        await context.bot.send_message(
            chat_id=customer_id,
            text=(
                "🎉 قیمت سفارش شما مشخص شد!\n\n"
                f"🆔 شماره سفارش:\n"
                f"{order.order_id}\n\n"
                "━━━━━━━━━━━━━━\n"
                "💰 مبلغ قابل پرداخت "
                "(مبلغ محصول + هزینه ارسال یا بسته‌بندی):\n\n"
                f"💵 {order.price}\n"
                "━━━━━━━━━━━━━━\n\n"
                "💳 لطفاً مبلغ بالا را به شماره کارت زیر "
                "واریز کنید:\n\n"
                f"{CARD_NUMBER}\n\n"
                "📸 بعد از پرداخت، تصویر رسید پرداخت را "
                "ارسال کنید."
            ),
            reply_markup=payment_keyboard(),
        )
    except Exception as send_error:
        print(
            "SEND PRICE ERROR:",
            repr(send_error),
        )

        order.state = WAITING_ADMIN_PRICE
        order.waiting_for_admin_price = True

        await message.reply_text(
            "❌ قیمت ذخیره شد اما ارسال آن برای مشتری "
            "با خطا مواجه شد.\n\n"
            f"خطا:\n{repr(send_error)}"
        )
        return

    await message.reply_text(
        "✅ قیمت با موفقیت ثبت شد و برای مشتری ارسال گردید.\n\n"
        f"🆔 سفارش: {order.order_id}\n"
        f"👤 مشتری: {customer_id}\n"
        f"💰 مبلغ: {order.price}"
    )


# =========================================================
# SEND RECEIPT TO ADMIN
# =========================================================


async def send_receipt_to_admin(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    order: OrderData,
):
    customer_name = (
        f"{order.first_name} {order.last_name}"
    ).strip()

    text = (
        "💳 رسید پرداخت جدید\n\n"
        f"🆔 شماره سفارش: {order.order_id}\n"
        f"👤 مشتری: {customer_name}\n"
        f"📱 شماره تماس: {order.phone}\n"
        f"💰 مبلغ: {order.price}\n\n"
        "⚠️ مشتری رسید پرداخت را ارسال کرده است."
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
    )

    if (
        order.receipt_type == "photo"
        and order.receipt_file_id
    ):
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=order.receipt_file_id,
            caption=(
                "🧾 رسید پرداخت\n"
                f"🆔 سفارش: {order.order_id}"
            ),
        )
        return

    if (
        order.receipt_type == "document"
        and order.receipt_file_id
    ):
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=order.receipt_file_id,
            caption=(
                "🧾 رسید پرداخت\n"
                f"🆔 سفارش: {order.order_id}"
            ),
        )
        return

    raise RuntimeError(
        "Receipt file type or file_id is missing."
    )


# =========================================================
# SEND FINAL ORDER TO ADMIN
# =========================================================


async def send_final_order_to_admin(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    order: OrderData,
):
    customer_name = (
        f"{order.first_name} {order.last_name}"
    ).strip()

    text = (
        "🛍 سفارش جدید سورین\n\n"
        f"🆔 شماره سفارش: {order.order_id}\n\n"
        "👤 اطلاعات مشتری\n"
        f"نام: {customer_name}\n"
        f"📱 شماره تماس: {order.phone}\n\n"
        "🛒 محصولات\n"
        f"{cart_text(order)}\n\n"
        "📍 آدرس کامل\n"
        f"{order.full_address}\n\n"
        f"🚚 روش دریافت: {order.shipping_method}\n"
        f"💸 هزینه دریافت: {get_shipping_fee(order):,} تومان\n\n"
        f"💰 مبلغ نهایی: {order.price}\n\n"
        "📝 توضیحات مشتری\n"
        f"{order.customer_description or 'ندارد'}\n\n"
        "💳 وضعیت پرداخت: رسید ارسال شده"
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
    )

    # محصولات
    for item in order.cart:
        if (
            item.product_type == "photo"
            and item.product_file_id
        ):
            try:
                await context.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=item.product_file_id,
                    caption=(
                        "📷 محصول سفارش\n"
                        f"🆔 {order.order_id}"
                    ),
                )
            except Exception as e:
                print(
                    "SEND PRODUCT PHOTO ERROR:",
                    repr(e),
                )

        elif (
            item.product_type == "document"
            and item.product_file_id
        ):
            try:
                await context.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=item.product_file_id,
                    caption=(
                        "📄 محصول سفارش\n"
                        f"🆔 {order.order_id}"
                    ),
                )
            except Exception as e:
                print(
                    "SEND PRODUCT DOCUMENT ERROR:",
                    repr(e),
                )

    # رسید
    if (
        order.receipt_file_id
        and order.receipt_type == "photo"
    ):
        try:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=order.receipt_file_id,
                caption=(
                    "🧾 رسید پرداخت\n\n"
                    f"🆔 سفارش: {order.order_id}"
                ),
            )
        except Exception as e:
            print(
                "SEND RECEIPT PHOTO ERROR:",
                repr(e),
            )

    elif (
        order.receipt_file_id
        and order.receipt_type == "document"
    ):
        try:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=order.receipt_file_id,
                caption=(
                    "🧾 رسید پرداخت\n\n"
                    f"🆔 سفارش: {order.order_id}"
                ),
            )
        except Exception as e:
            print(
                "SEND RECEIPT DOCUMENT ERROR:",
                repr(e),
            )


# =========================================================
# CALLBACK HANDLER
# =========================================================


async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user
    if not user:
        return

    user_id = user.id
    data = query.data or ""

    # پیام اصلی که دکمه زیر آن قرار دارد
    message = query.message

    # -----------------------------------------------------
    # CANCEL
    # -----------------------------------------------------

    if data == "cancel_order":
        reset_order(user_id)

        await message.reply_text(
            "❌ سفارش لغو شد.\n\n"
            "برای شروع دوباره، روی دکمه زیر بزنید.",
            reply_markup=main_start_keyboard(),
        )
        return

    # -----------------------------------------------------
    # START
    # -----------------------------------------------------

    if data == "start_purchase":
        reset_order(user_id)

        order = get_order(user_id)
        order.state = NAME

        await message.reply_text(
            "عالیه 🌱\n\n"
            "برای شروع ثبت سفارش، لطفاً نام و نام خانوادگی "
            "خود را وارد کنید.",
            reply_markup=cancel_keyboard(),
        )
        return

    order = get_order(user_id)

    # -----------------------------------------------------
    # PRODUCT
    # -----------------------------------------------------

    if data == "add_product":
        if order.state not in {PRODUCT, CART}:
            return

        order.state = PRODUCT

        await message.reply_text(
            "🛍 محصول جدید را ارسال کنید:\n\n"
            "• عکس\n"
            "• کد محصول\n"
            "• نام محصول\n"
            "• لینک محصول",
            reply_markup=cancel_keyboard(),
        )
        return

    if data == "show_cart":
        if order.state not in {PRODUCT, CART}:
            return

        await show_cart(update, context)
        return

    if data == "continue_order":
        if order.state != CART:
            return

        if not order.cart:
            await message.reply_text(
                "سبد خرید شما خالی است.",
                reply_markup=cart_keyboard(),
            )
            return

        order.state = ADDRESS
        await ask_address(update, context)
        return

    # -----------------------------------------------------
    # PRODUCT DETAILS
    # -----------------------------------------------------

    if data == "no_product_details":
        if order.state != PRODUCT_DETAILS:
            return

        if not order.cart:
            order.state = PRODUCT
            await ask_product(update, context)
            return

        order.cart[-1].details = ""
        order.state = CART

        await show_cart(update, context)
        return

    # -----------------------------------------------------
    # SHIPPING
    # -----------------------------------------------------

    if data == "shipping_post":
        if order.state != SHIPPING:
            return

        order.shipping_method = SHIPPING_POST
        order.state = CUSTOMER_DESCRIPTION

        await ask_customer_description(update, context)
        return

    if data == "shipping_pickup":
        if order.state != SHIPPING:
            return

        order.shipping_method = SHIPPING_PICKUP
        order.state = CUSTOMER_DESCRIPTION

        await ask_customer_description(update, context)
        return

    # -----------------------------------------------------
    # CUSTOMER DESCRIPTION
    # -----------------------------------------------------

    if data == "no_customer_description":
        if order.state != CUSTOMER_DESCRIPTION:
            return

        order.customer_description = ""
        order.state = PRICE_STATUS

        await ask_price_status(update, context)
        return

    # -----------------------------------------------------
    # PRICE STATUS
    # -----------------------------------------------------

    if data == "price_known":
        if order.state != PRICE_STATUS:
            return

        order.price_known = True
        order.state = PAYMENT

        await message.reply_text(
            "💰 لطفاً قیمت خودِ محصولات را وارد کنید.\n\n"
            "⚠️ هزینه دریافت سفارش به‌صورت خودکار اضافه می‌شود.\n"
            "• پست: ۲۰۰٬۰۰۰ تومان\n"
            "• دریافت توسط مشتری: ۴۵٬۰۰۰ تومان هزینه بسته‌بندی\n\n"
            "مثال:\n"
            "850000",
            reply_markup=cancel_keyboard(),
        )
        return

    if data == "price_unknown":
        if order.state != PRICE_STATUS:
            return

        order.price_known = False

        await send_price_request_to_admin(
            context,
            user_id,
            order,
        )

        await message.reply_text(
            "✅ اطلاعات سفارش شما برای فروشگاه ارسال شد.\n\n"
            "💰 قیمت سفارش توسط فروشگاه بررسی می‌شود.\n"
            "بعد از تعیین قیمت، مبلغ و اطلاعات پرداخت "
            "برای شما ارسال خواهد شد.\n\n"
            f"🆔 شماره سفارش:\n"
            f"{order.order_id}",
            reply_markup=cancel_keyboard(),
        )
        return

    # -----------------------------------------------------
    # PAYMENT
    # -----------------------------------------------------

    if data == "send_receipt":
        if order.state != RECEIPT:
            # اگر هنوز مبلغ دستی وارد نشده، در حالت PAYMENT هستیم
            if order.state == PAYMENT:
                await message.reply_text(
                    "📸 لطفاً ابتدا قیمت خودِ محصولات را وارد کنید "
                    "و سپس رسید را ارسال کنید.",
                    reply_markup=cancel_keyboard(),
                )
            return

        await message.reply_text(
            "📸 لطفاً تصویر رسید پرداخت را ارسال کنید.",
            reply_markup=cancel_keyboard(),
        )
        return

    # -----------------------------------------------------
    # CONFIRM
    # -----------------------------------------------------

    if data == "confirm_order":
        if order.state != CONFIRM:
            return

        await send_final_order_to_admin(
            context,
            user_id,
            order,
        )

        order.state = START

        await message.reply_text(
            "🎉 سفارش شما با موفقیت ثبت شد.\n\n"
            f"🆔 شماره سفارش:\n"
            f"{order.order_id}\n\n"
            "✅ سفارش شما برای فروشگاه ارسال شد.\n"
            "در صورت نیاز، فروشگاه با شما تماس خواهد گرفت.\n\n"
            "از خرید شما از سورین ممنونیم 🌱",
            reply_markup=main_start_keyboard(),
        )
        return

    if data == "edit_order":
        if order.state != CONFIRM:
            return

        reset_order(user_id)

        order = get_order(user_id)
        order.state = NAME

        await message.reply_text(
            "✏️ ویرایش سفارش شروع شد.\n\n"
            "لطفاً نام و نام خانوادگی را دوباره وارد کنید.",
            reply_markup=cancel_keyboard(),
        )
        return


# =========================================================
# START COMMAND
# =========================================================


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await send_welcome(update, context)


async def reset_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await send_welcome(update, context)


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await send_welcome(update, context)


# =========================================================
# CUSTOMER MESSAGE HANDLER
# =========================================================


async def customer_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    user_id = user.id

    # =====================================================
    # ADMIN
    # =====================================================

    if user_id == ADMIN_ID:
        await process_admin_price(
            update,
            context,
        )
        return

    # =====================================================
    # TEXT
    # =====================================================

    text = normalize_text(
        message.text or ""
    )

    normalized = text.lower()

    # =====================================================
    # START
    # =====================================================

    if normalized in [
        "/start",
        "شروع",
    ]:
        await send_welcome(update, context)
        return

    # =====================================================
    # CANCEL
    # =====================================================

    if normalized in [
        "/cancel",
        "cancel",
        "لغو سفارش",
    ]:
        await send_welcome(update, context)
        return

    # =====================================================
    # RESET
    # =====================================================

    if normalized in [
        "/reset",
        "شروع مجدد",
    ]:
        await send_welcome(update, context)
        return

    order = get_order(user_id)

    # =====================================================
    # START STATE
    # =====================================================

    if order.state == START:
        await message.reply_text(
            "لطفاً برای شروع ثبت سفارش، "
            "روی دکمه «🛍 شروع خرید» بزنید.",
            reply_markup=main_start_keyboard(),
        )
        return

    # =====================================================
    # NAME
    # =====================================================

    if order.state == NAME:
        if not text:
            await message.reply_text(
                "لطفاً نام و نام خانوادگی خود را وارد کنید.",
                reply_markup=cancel_keyboard(),
            )
            return

        parts = text.split(maxsplit=1)

        order.first_name = parts[0]

        if len(parts) > 1:
            order.last_name = parts[1]
        else:
            order.last_name = ""

        order.state = PHONE

        await ask_phone(update, context)
        return

    # =====================================================
    # PHONE
    # =====================================================

    if order.state == PHONE:
        if not text:
            await message.reply_text(
                "لطفاً شماره تماس خود را به‌صورت متنی ارسال کنید.",
                reply_markup=cancel_keyboard(),
            )
            return

        # عمداً Contact Button نداریم.
        # شماره فقط به صورت متن دریافت می‌شود.
        order.phone = text

        order.state = PRODUCT

        await ask_product(update, context)
        return

    # =====================================================
    # PRODUCT
    # =====================================================

    if order.state == PRODUCT:
        if normalized in [
            "مشاهده سبد خرید",
        ]:
            await show_cart(update, context)
            return

        if normalized in [
            "افزودن محصول",
        ]:
            await message.reply_text(
                "🛍 محصول جدید را ارسال کنید:\n\n"
                "• عکس\n"
                "• کد محصول\n"
                "• نام محصول\n"
                "• لینک محصول",
                reply_markup=cancel_keyboard(),
            )
            return

        # PHOTO
        photo_file_id = get_photo_file_id(message)

        if photo_file_id:
            item = CartItem(
                product_type="photo",
                product_file_id=photo_file_id,
                product_text="محصول تصویری",
            )

            order.cart.append(item)
            order.state = PRODUCT_DETAILS

            await ask_product_details(update, context)
            return

        # DOCUMENT
        document_file_id = get_document_file_id(message)

        if document_file_id:
            item = CartItem(
                product_type="document",
                product_file_id=document_file_id,
                product_text="محصول ارسالی",
            )

            order.cart.append(item)
            order.state = PRODUCT_DETAILS

            await ask_product_details(update, context)
            return

        # TEXT PRODUCT
        if text:
            item = CartItem(
                product_type="text",
                product_text=text,
            )

            order.cart.append(item)
            order.state = PRODUCT_DETAILS

            await ask_product_details(update, context)
            return

        await message.reply_text(
            "لطفاً عکس، کد، نام یا لینک محصول را ارسال کنید.",
            reply_markup=cancel_keyboard(),
        )
        return

    # =====================================================
    # PRODUCT DETAILS
    # =====================================================

    if order.state == PRODUCT_DETAILS:
        if not order.cart:
            order.state = PRODUCT
            await ask_product(update, context)
            return

        order.cart[-1].details = text

        order.state = CART

        await show_cart(update, context)
        return

    # =====================================================
    # CART
    # =====================================================

    if order.state == CART:
        await message.reply_text(
            "لطفاً از دکمه‌های زیر استفاده کنید.",
            reply_markup=cart_keyboard(),
        )
        return

    # =====================================================
    # ADDRESS
    # =====================================================

    if order.state == ADDRESS:
        if not text:
            await message.reply_text(
                "لطفاً آدرس کامل را در یک پیام ارسال کنید.",
                reply_markup=cancel_keyboard(),
            )
            return

        order.full_address = text
        order.state = SHIPPING

        await ask_shipping(update, context)
        return

    # =====================================================
    # SHIPPING
    # =====================================================

    if order.state == SHIPPING:
        await message.reply_text(
            "لطفاً یکی از روش‌های دریافت را از دکمه‌های زیر انتخاب کنید.",
            reply_markup=shipping_keyboard(),
        )
        return

    # =====================================================
    # CUSTOMER DESCRIPTION
    # =====================================================

    if order.state == CUSTOMER_DESCRIPTION:
        order.customer_description = text
        order.state = PRICE_STATUS

        await ask_price_status(update, context)
        return

    # =====================================================
    # PRICE STATUS
    # =====================================================

    if order.state == PRICE_STATUS:
        await message.reply_text(
            "لطفاً یکی از گزینه‌های قیمت را انتخاب کنید.",
            reply_markup=price_keyboard(),
        )
        return

    # =====================================================
    # PAYMENT
    # =====================================================

    if order.state == PAYMENT:
        # اگر کاربر قیمت را می‌داند، مبلغ محصولات را دستی وارد می‌کند.
        price = normalize_price(text)

        if not price:
            await message.reply_text(
                "❌ مبلغ واردشده معتبر نیست.\n\n"
                "مثال:\n"
                "850000",
                reply_markup=cancel_keyboard(),
            )
            return

        try:
            order.price = add_shipping_fee(
                order,
                price,
            )
        except ValueError:
            await message.reply_text(
                "❌ مبلغ واردشده معتبر نیست.\n\n"
                "مثال:\n"
                "850000",
                reply_markup=cancel_keyboard(),
            )
            return

        order.state = RECEIPT

        await ask_payment(update, context)
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
                "📸 لطفاً تصویر رسید پرداخت را ارسال کنید.",
                reply_markup=cancel_keyboard(),
            )
            return

        # ارسال رسید به ادمین
        try:
            await send_receipt_to_admin(
                context,
                user_id,
                order,
            )
        except Exception as e:
            print(
                "SEND RECEIPT TO ADMIN ERROR:",
                repr(e),
            )

        order.state = CONFIRM

        await message.reply_text(
            customer_order_summary(order),
            reply_markup=confirm_keyboard(),
        )
        return

    # =====================================================
    # CONFIRM
    # =====================================================

    if order.state == CONFIRM:
        await message.reply_text(
            "لطفاً اطلاعات سفارش را بررسی کنید و "
            "یکی از گزینه‌های زیر را انتخاب کنید.",
            reply_markup=confirm_keyboard(),
        )
        return

    # =====================================================
    # WAITING ADMIN PRICE
    # =====================================================

    if order.state == WAITING_ADMIN_PRICE:
        await message.reply_text(
            "⏳ سفارش شما در انتظار تعیین قیمت توسط فروشگاه است.\n\n"
            f"🆔 شماره سفارش:\n"
            f"{order.order_id}\n\n"
            "بعد از مشخص شدن قیمت، اطلاعات پرداخت "
            "برای شما ارسال می‌شود.",
            reply_markup=cancel_keyboard(),
        )
        return


# =========================================================
# ERROR HANDLER
# =========================================================


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    print(
        "BOT ERROR:",
        repr(context.error),
    )


# =========================================================
# MAIN
# =========================================================


def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(False)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "reset",
            reset_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
        )
    )

    # تمام دکمه‌های Inline
    application.add_handler(
        CallbackQueryHandler(
            callback_handler,
        )
    )

    # پیام‌های متنی و فایل‌ها
    application.add_handler(
        MessageHandler(
            filters.ALL,
            customer_message_handler,
        )
    )

    application.add_error_handler(error_handler)

    return application


if __name__ == "__main__":
    print("===================================")
    print("Suryan Telegram Order Bot")
    print("Bot is starting...")
    print("===================================")

    app = build_application()

    # Polling
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
