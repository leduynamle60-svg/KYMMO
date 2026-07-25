import os
import re
import secrets
import json
import urllib.request
import urllib.error
import mimetypes
from urllib.parse import quote, unquote, urlparse
from datetime import datetime, timedelta, timezone
from functools import wraps
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from authlib.integrations.flask_client import OAuth
    from authlib.integrations.base_client.errors import MismatchingStateError
except ImportError:
    OAuth = None
    MismatchingStateError = Exception

from mail import (
    mail,
    send_order_confirmation_email,
    send_otp_email,
    send_seller_status_email,
)


load_dotenv()


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["PREFERRED_URL_SCHEME"] = "https"

VN_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

def to_vietnam_time(value):
    """Convert a stored UTC datetime to Asia/Ho_Chi_Minh for display."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(VN_TIMEZONE)

@app.template_filter("vn_time")
def vn_time_filter(value, fmt="%d/%m/%Y %H:%M"):
    converted = to_vietnam_time(value)
    return converted.strftime(fmt) if converted else ""


ALLOWED_PRODUCT_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
app.config["MAX_CONTENT_LENGTH"] = 120 * 1024 * 1024

# Supabase Storage. Bucket must be PUBLIC so product images can be displayed.
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_STORAGE_KEY = (
    os.getenv("SUPABASE_SECRET_KEY")
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or ""
)
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "product-images").strip()

# =========================================================
# CẤU HÌNH CHUNG
# =========================================================

secret_key = os.getenv("SECRET_KEY")

if not secret_key:
    raise RuntimeError(
        "Thiếu SECRET_KEY trong file .env. "
        "Hãy tạo SECRET_KEY trước khi chạy ứng dụng."
    )

app.config["SECRET_KEY"] = secret_key

# Session cookie
app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# Khi deploy HTTPS thật trên Render thì đổi thành True.
app.config["SESSION_COOKIE_SECURE"] = (
    os.getenv("SESSION_COOKIE_SECURE", "False").lower() == "true"
)

# =========================================================
# DATABASE
# =========================================================

database_url = os.getenv("DATABASE_URL")

if not database_url:
    raise RuntimeError(
        "Thiếu DATABASE_URL trong file .env."
    )

# Một số dịch vụ PostgreSQL vẫn trả về postgres://
# SQLAlchemy cần postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace(
        "postgres://",
        "postgresql://",
        1,
    )

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}

db = SQLAlchemy(app)

# OAuth xã hội là tùy chọn: web vẫn chạy bình thường khi chưa cấu hình.
oauth = OAuth(app) if OAuth else None

# =========================================================
# RESEND EMAIL API
# =========================================================

# Resend sử dụng HTTPS API nên chạy được trên Render Free.
# RESEND_FROM_EMAIL có thể để mặc định onboarding@resend.dev khi test.
if not os.getenv("RESEND_API_KEY"):
    raise RuntimeError(
        "Thiếu RESEND_API_KEY trong file .env."
    )

mail.init_app(app)

# =========================================================
# THIẾT LẬP OTP
# =========================================================

OTP_EXPIRE_MINUTES = 5
OTP_RESEND_SECONDS = 300
OTP_MAX_ATTEMPTS = 5

VALID_OTP_PURPOSES = {
    "register",
    "forgot_password",
}


# =========================================================
# MODEL DATABASE
# =========================================================

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    username = db.Column(
        db.String(50),
        unique=True,
        nullable=False,
        index=True,
    )

    email = db.Column(
        db.String(255),
        unique=True,
        nullable=False,
        index=True,
    )

    password_hash = db.Column(
        db.String(255),
        nullable=False,
    )

    role = db.Column(
        db.String(30),
        nullable=False,
        default="buyer",
    )

    balance = db.Column(
        db.BigInteger,
        nullable=False,
        default=0,
    )

    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    google_id = db.Column(db.String(255), unique=True, index=True)
    discord_id = db.Column(db.String(255), unique=True, index=True)
    avatar_url = db.Column(db.Text)
    seller_welcome_pending = db.Column(db.Boolean, nullable=False, default=False)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(
            self.password_hash,
            password,
        )


class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    category = db.Column(db.String(60), nullable=False, default="Dịch vụ")
    image_url = db.Column(db.Text)
    price = db.Column(db.BigInteger, nullable=False, default=0)
    seller_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    canonical_key = db.Column(db.String(180), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class SitePage(db.Model):
    __tablename__ = "site_pages"
    id = db.Column(db.Integer, primary_key=True)
    page_key = db.Column(db.String(50), unique=True, nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class SiteSetting(db.Model):
    __tablename__ = "site_settings"
    id = db.Column(db.Integer, primary_key=True)
    setting_key = db.Column(db.String(80), unique=True, nullable=False, index=True)
    setting_value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


SITE_SETTING_DEFAULTS = {
    "footer_description": "Marketplace dành cho tài khoản, gift card và dịch vụ số. Giao dịch minh bạch, seller được xét duyệt và hỗ trợ rõ ràng.",
    "discord_url": "https://discord.gg/3ES353BXY9",
    "support_email": "support@kymmo.shop",
    "footer_note": "Tham gia Discord để được hỗ trợ nhanh nhất.",
    "footer_copyright": "© 2026 KY MMO. All rights reserved.",
    "footer_slogan": "Uy tín · Minh bạch · Hỗ trợ nhanh",
    "announcement_lines": "Seller được xét duyệt\nGiao hàng tự động 24/7\nThông tin sản phẩm minh bạch\nHỗ trợ nhanh chóng\nThanh toán bằng ví an toàn",
}

def get_site_settings():
    rows = {row.setting_key: row.setting_value for row in SiteSetting.query.all()}
    return {key: rows.get(key, default) for key, default in SITE_SETTING_DEFAULTS.items()}


class SellerRequest(db.Model):
    __tablename__="seller_requests"
    id=db.Column(db.Integer,primary_key=True); user_id=db.Column(db.Integer,db.ForeignKey("users.id"),nullable=False,index=True)
    shop_name=db.Column(db.String(120),nullable=False); full_name=db.Column(db.String(120),nullable=False); email=db.Column(db.String(255),nullable=False); phone=db.Column(db.String(30),nullable=False); category=db.Column(db.String(120),nullable=False); description=db.Column(db.Text,nullable=False)
    status=db.Column(db.String(20),nullable=False,default="pending",index=True); admin_note=db.Column(db.Text); processed_by=db.Column(db.Integer); processed_at=db.Column(db.DateTime(timezone=True)); created_at=db.Column(db.DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),nullable=False)

class Shop(db.Model):
    __tablename__="shops"
    id=db.Column(db.Integer,primary_key=True); seller_id=db.Column(db.Integer,db.ForeignKey("users.id"),nullable=False,unique=True,index=True)
    name=db.Column(db.String(120),nullable=False); slug=db.Column(db.String(160),nullable=False,unique=True,index=True); description=db.Column(db.Text,nullable=False); support_info=db.Column(db.Text,nullable=False); avatar_url=db.Column(db.Text); status=db.Column(db.String(20),default="active",nullable=False,index=True); rating_average=db.Column(db.Float,default=0.0,nullable=False); rating_count=db.Column(db.Integer,default=0,nullable=False); created_at=db.Column(db.DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),nullable=False)

class SellerWallet(db.Model):
    __tablename__="seller_wallets"
    id=db.Column(db.Integer,primary_key=True); seller_id=db.Column(db.Integer,db.ForeignKey("users.id"),nullable=False,unique=True,index=True); available_balance=db.Column(db.BigInteger,default=0,nullable=False); pending_balance=db.Column(db.BigInteger,default=0,nullable=False); withdrawal_hold=db.Column(db.BigInteger,default=0,nullable=False); total_earned=db.Column(db.BigInteger,default=0,nullable=False)

class SellerHold(db.Model):
    __tablename__="seller_holds"
    id=db.Column(db.Integer,primary_key=True); seller_id=db.Column(db.Integer,db.ForeignKey("users.id"),nullable=False,index=True); order_reference=db.Column(db.String(100),index=True); gross_amount=db.Column(db.BigInteger,default=0,nullable=False); platform_fee=db.Column(db.BigInteger,default=0,nullable=False); seller_amount=db.Column(db.BigInteger,default=0,nullable=False); status=db.Column(db.String(20),default="holding",nullable=False,index=True); held_at=db.Column(db.DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),nullable=False); release_at=db.Column(db.DateTime(timezone=True),nullable=False); released_at=db.Column(db.DateTime(timezone=True))

class WithdrawalRequest(db.Model):
    __tablename__="withdrawal_requests"
    id=db.Column(db.Integer,primary_key=True); seller_id=db.Column(db.Integer,db.ForeignKey("users.id"),nullable=False,index=True); amount=db.Column(db.BigInteger,nullable=False); bank_name=db.Column(db.String(100),nullable=False); account_number=db.Column(db.String(100),nullable=False); account_name=db.Column(db.String(150),nullable=False); status=db.Column(db.String(20),default="pending",nullable=False,index=True); admin_note=db.Column(db.Text); processed_by=db.Column(db.Integer); processed_at=db.Column(db.DateTime(timezone=True)); created_at=db.Column(db.DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),nullable=False)

class Review(db.Model):
    __tablename__="reviews"
    id=db.Column(db.Integer,primary_key=True); order_reference=db.Column(db.String(100),nullable=False,unique=True,index=True); buyer_id=db.Column(db.Integer,db.ForeignKey("users.id"),nullable=False,index=True); shop_id=db.Column(db.Integer,db.ForeignKey("shops.id"),nullable=False,index=True); rating=db.Column(db.Integer,nullable=False); content=db.Column(db.Text,nullable=False); created_at=db.Column(db.DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),nullable=False)

class Report(db.Model):
    __tablename__="reports"
    id=db.Column(db.Integer,primary_key=True); reporter_id=db.Column(db.Integer,db.ForeignKey("users.id"),nullable=False,index=True); shop_id=db.Column(db.Integer,db.ForeignKey("shops.id"),nullable=False,index=True); order_reference=db.Column(db.String(100),nullable=False); reason=db.Column(db.String(200),nullable=False); description=db.Column(db.Text,nullable=False); status=db.Column(db.String(20),default="pending",nullable=False,index=True); created_at=db.Column(db.DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),nullable=False)

class ProductDelivery(db.Model):
    __tablename__ = "product_deliveries"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, unique=True, index=True)
    delivery_type = db.Column(db.String(20), nullable=False, default="manual")  # automatic/manual
    instructions = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class ProductStock(db.Model):
    __tablename__ = "product_stocks"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="available", index=True)
    order_id = db.Column(db.Integer, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    sold_at = db.Column(db.DateTime(timezone=True))


class MarketplaceOrder(db.Model):
    __tablename__ = "marketplace_orders"
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(40), nullable=False, unique=True, index=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    amount = db.Column(db.BigInteger, nullable=False)
    delivery_type = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="paid", index=True)
    delivered_content = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    delivered_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))


class TicketMessage(db.Model):
    __tablename__ = "ticket_messages"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("marketplace_orders.id"), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class RefundRequest(db.Model):
    __tablename__ = "refund_requests"
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(40), nullable=False, unique=True, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("marketplace_orders.id"), nullable=False, index=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), nullable=False, default="pending", index=True)
    seller_decision = db.Column(db.String(20), nullable=False, default="pending")
    seller_note = db.Column(db.Text)
    admin_decision = db.Column(db.String(20), nullable=False, default="pending")
    admin_note = db.Column(db.Text)
    processed_by = db.Column(db.Integer)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    attachments = db.relationship("RefundAttachment", backref="refund_request", lazy=True, cascade="all, delete-orphan")


class RefundAttachment(db.Model):
    __tablename__ = "refund_attachments"
    id = db.Column(db.Integer, primary_key=True)
    refund_id = db.Column(db.Integer, db.ForeignKey("refund_requests.id"), nullable=False, index=True)
    file_url = db.Column(db.Text, nullable=False)
    file_type = db.Column(db.String(20), nullable=False)
    original_name = db.Column(db.String(255))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class OtpCode(db.Model):
    __tablename__ = "otp_codes"

    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    email = db.Column(
        db.String(255),
        nullable=False,
        index=True,
    )

    purpose = db.Column(
        db.String(50),
        nullable=False,
        index=True,
    )

    code_hash = db.Column(
        db.String(255),
        nullable=False,
    )

    attempts = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    is_used = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    expires_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# =========================================================
# HÀM HỖ TRỢ
# =========================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_username(username: str) -> str:
    return username.strip()


def is_valid_gmail(email: str) -> bool:
    pattern = r"^[^\s@]+@gmail\.com$"
    return bool(
        re.fullmatch(
            pattern,
            email,
            flags=re.IGNORECASE,
        )
    )


def is_valid_username(username: str) -> bool:
    if len(username) < 4 or len(username) > 30:
        return False

    return bool(
        re.fullmatch(
            r"[A-Za-z0-9_.]+",
            username,
        )
    )


def json_error(
    message: str,
    status_code: int = 400,
):
    return jsonify({
        "success": False,
        "message": message,
    }), status_code


def json_success(
    message: str,
    **extra,
):
    payload = {
        "success": True,
        "message": message,
    }

    payload.update(extra)

    return jsonify(payload)


def get_json_data() -> dict:
    data = request.get_json(silent=True)

    if isinstance(data, dict):
        return data

    return {}


def generate_otp_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def get_latest_otp(
    email: str,
    purpose: str,
):
    return (
        OtpCode.query
        .filter_by(
            email=email,
            purpose=purpose,
            is_used=False,
        )
        .order_by(OtpCode.created_at.desc())
        .first()
    )


def create_and_send_otp(
    email: str,
    purpose: str,
) -> tuple[bool, str]:
    if purpose not in VALID_OTP_PURPOSES:
        return False, "Loại OTP không hợp lệ."

    now = utc_now()

    latest = get_latest_otp(
        email,
        purpose,
    )

    if latest:
        created_at = latest.created_at

        if created_at.tzinfo is None:
            created_at = created_at.replace(
                tzinfo=timezone.utc,
            )

        elapsed = (
            now - created_at
        ).total_seconds()

        if elapsed < OTP_RESEND_SECONDS:
            wait_seconds = int(
                OTP_RESEND_SECONDS - elapsed
            )

            return (
                False,
                f"Vui lòng chờ {wait_seconds} giây "
                "trước khi gửi lại mã.",
            )

    otp = generate_otp_code()

    # Chỉ lưu/vô hiệu hóa mã cũ SAU KHI Resend nhận email thành công.
    # Như vậy nếu nhà cung cấp email lỗi, mã OTP cũ vẫn còn dùng được.
    try:
        resend_email_id = send_otp_email(
            email=email,
            otp=otp,
            purpose=purpose,
            expire_minutes=OTP_EXPIRE_MINUTES,
        )
    except Exception as exc:
        app.logger.exception(
            "Không gửi được email OTP tới %s: %s",
            email,
            exc,
        )
        return (
            False,
            "Không thể gửi email lúc này. Vui lòng thử lại sau ít phút.",
        )

    try:
        (
            OtpCode.query
            .filter_by(
                email=email,
                purpose=purpose,
                is_used=False,
            )
            .update({"is_used": True})
        )

        otp_record = OtpCode(
            email=email,
            purpose=purpose,
            code_hash=generate_password_hash(otp),
            attempts=0,
            is_used=False,
            expires_at=now + timedelta(minutes=OTP_EXPIRE_MINUTES),
        )
        db.session.add(otp_record)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Không lưu được OTP cho %s", email)
        return False, "Không thể tạo mã OTP lúc này. Vui lòng thử lại."

    app.logger.info(
        "OTP đã được Resend chấp nhận: email=%s resend_id=%s",
        email,
        resend_email_id or "unknown",
    )
    return True, (
        "Đã gửi mã OTP. Vui lòng kiểm tra Hộp thư đến, "
        "Quảng cáo và Thư rác."
    )


def verify_otp_code(
    email: str,
    purpose: str,
    otp: str,
    mark_used: bool = False,
) -> tuple[bool, str]:
    otp_record = get_latest_otp(
        email,
        purpose,
    )

    if not otp_record:
        return (
            False,
            "Không tìm thấy mã OTP. "
            "Vui lòng gửi lại mã.",
        )

    expires_at = otp_record.expires_at

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(
            tzinfo=timezone.utc,
        )

    if utc_now() > expires_at:
        otp_record.is_used = True
        db.session.commit()

        return (
            False,
            "Mã OTP đã hết hạn. "
            "Vui lòng gửi lại mã mới.",
        )

    if otp_record.attempts >= OTP_MAX_ATTEMPTS:
        otp_record.is_used = True
        db.session.commit()

        return (
            False,
            "Bạn đã nhập sai quá nhiều lần. "
            "Vui lòng gửi lại mã mới.",
        )

    if not check_password_hash(
        otp_record.code_hash,
        otp,
    ):
        otp_record.attempts += 1
        db.session.commit()

        remaining = max(
            0,
            OTP_MAX_ATTEMPTS - otp_record.attempts,
        )

        return (
            False,
            f"Mã OTP không đúng. "
            f"Còn {remaining} lần thử.",
        )

    if mark_used:
        otp_record.is_used = True
        db.session.commit()

    return True, "OTP hợp lệ."


def login_required(view_function):
    @wraps(view_function)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(
                url_for(
                    "auth",
                    mode="login",
                )
            )

        return view_function(
            *args,
            **kwargs,
        )

    return wrapped


def current_user():
    user_id = session.get("user_id")

    if not user_id:
        return None

    return db.session.get(
        User,
        user_id,
    )


def normalized_role(user):
    if not user:
        return "guest"

    role = str(getattr(user, "role", "buyer") or "buyer").strip().lower()

    if role == "owner":
        return "founder"

    if role in {"member", "user"}:
        return "buyer"

    return role


def is_founder(user):
    return normalized_role(user) == "founder"


def is_admin_or_founder(user):
    return normalized_role(user) in {"founder", "admin"}


def founder_required(view_function):
    @wraps(view_function)
    def wrapped(*args, **kwargs):
        user = current_user()

        if not user:
            return redirect(url_for("auth", mode="login"))

        if not is_founder(user):
            flash("Chỉ Founder mới có quyền sử dụng chức năng này.", "error")
            return redirect(url_for("profile"))

        return view_function(*args, **kwargs)

    return wrapped



def send_discord_webhook(title: str, description: str, color: int = 0x2F80ED) -> tuple[bool, str]:
    webhook_url = (
        os.getenv("SELLER_REQUEST_WEBHOOK_URL")
        or os.getenv("DISCORD_WEBHOOK_URL")
        or ""
    ).strip()

    if not webhook_url:
        app.logger.warning("Chưa cấu hình SELLER_REQUEST_WEBHOOK_URL trong .env")
        return False, "Thiếu SELLER_REQUEST_WEBHOOK_URL trong .env."

    payload = {
        "username": "KY MMO Marketplace",
        "embeds": [{
            "title": title,
            "description": description[:4000],
            "color": color,
            "timestamp": utc_now().isoformat(),
            "footer": {"text": "KY MMO Seller System"},
        }],
    }
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "KY-MMO/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status not in {200, 204}:
                return False, f"Discord trả về HTTP {response.status}."
        return True, "Đã gửi Discord webhook."
    except Exception as exc:
        app.logger.exception("Không gửi được Discord webhook")
        return False, str(exc)


def slugify_shop(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or secrets.token_hex(4)


def release_due_holds() -> int:
    now = utc_now()
    holds = SellerHold.query.filter(SellerHold.status == "holding", SellerHold.release_at <= now).all()
    released = 0
    for hold in holds:
        wallet = SellerWallet.query.filter_by(seller_id=hold.seller_id).first()
        if not wallet:
            wallet = SellerWallet(seller_id=hold.seller_id)
            db.session.add(wallet)
        wallet.pending_balance = max(0, int(wallet.pending_balance or 0) - int(hold.seller_amount))
        wallet.available_balance = int(wallet.available_balance or 0) + int(hold.seller_amount)
        wallet.total_earned = int(wallet.total_earned or 0) + int(hold.seller_amount)
        hold.status = "released"
        hold.released_at = now
        released += 1
    if released:
        db.session.commit()
    return released


def admin_required(view_function):
    @wraps(view_function)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("auth", mode="login"))
        if not is_admin_or_founder(user):
            flash("Bạn không có quyền truy cập.", "error")
            return redirect(url_for("profile"))
        return view_function(*args, **kwargs)
    return wrapped

def _require_supabase_storage_config():
    missing = []
    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not SUPABASE_STORAGE_KEY:
        missing.append("SUPABASE_SECRET_KEY hoặc SUPABASE_SERVICE_ROLE_KEY")
    if not SUPABASE_STORAGE_BUCKET:
        missing.append("SUPABASE_STORAGE_BUCKET")
    if missing:
        raise RuntimeError("Thiếu cấu hình Supabase Storage: " + ", ".join(missing))


def _supabase_storage_request(method, object_path, data=None, content_type=None):
    """Gọi Supabase Storage REST API từ server; secret key không lộ ra trình duyệt."""
    _require_supabase_storage_config()
    encoded_bucket = quote(SUPABASE_STORAGE_BUCKET, safe="")
    encoded_path = quote(object_path.lstrip("/"), safe="/")
    endpoint = f"{SUPABASE_URL}/storage/v1/object/{encoded_bucket}/{encoded_path}"
    headers = {
        "apikey": SUPABASE_STORAGE_KEY,
        "Authorization": f"Bearer {SUPABASE_STORAGE_KEY}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    if method in {"POST", "PUT"}:
        headers["x-upsert"] = "false"
    request_object = urllib.request.Request(
        endpoint,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request_object, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        app.logger.error("Supabase Storage HTTP %s: %s", exc.code, details)
        raise RuntimeError("Supabase Storage từ chối tải ảnh. Kiểm tra bucket và API key.") from exc
    except urllib.error.URLError as exc:
        app.logger.error("Không kết nối được Supabase Storage: %s", exc)
        raise RuntimeError("Không thể kết nối Supabase Storage. Hãy thử lại.") from exc


def _public_storage_url(object_path):
    encoded_bucket = quote(SUPABASE_STORAGE_BUCKET, safe="")
    encoded_path = quote(object_path.lstrip("/"), safe="/")
    return f"{SUPABASE_URL}/storage/v1/object/public/{encoded_bucket}/{encoded_path}"


def _storage_path_from_public_url(image_url):
    """Lấy object path từ URL do chính bucket hiện tại tạo ra."""
    if not image_url or not SUPABASE_URL or not SUPABASE_STORAGE_BUCKET:
        return None
    expected_prefix = (
        f"{SUPABASE_URL}/storage/v1/object/public/"
        f"{quote(SUPABASE_STORAGE_BUCKET, safe='')}/"
    )
    if not image_url.startswith(expected_prefix):
        return None
    return unquote(image_url[len(expected_prefix):].split("?", 1)[0])


def delete_product_image(image_url):
    object_path = _storage_path_from_public_url(image_url)
    if not object_path:
        return False
    try:
        _supabase_storage_request("DELETE", object_path)
        return True
    except RuntimeError:
        app.logger.exception("Không thể xóa ảnh cũ trên Supabase Storage")
        return False


def save_product_image(file_storage):
    """Upload ảnh sản phẩm lên Supabase Storage và trả về public URL."""
    if not file_storage or not file_storage.filename:
        return None

    original = secure_filename(file_storage.filename)
    if "." not in original:
        raise ValueError("Ảnh sản phẩm không hợp lệ.")
    extension = original.rsplit(".", 1)[1].lower()
    if extension not in ALLOWED_PRODUCT_IMAGE_EXTENSIONS:
        raise ValueError("Chỉ hỗ trợ ảnh PNG, JPG, JPEG hoặc WEBP.")

    payload = file_storage.read()
    if not payload:
        raise ValueError("Tệp ảnh đang trống.")
    if len(payload) > app.config["MAX_CONTENT_LENGTH"]:
        raise ValueError("Ảnh sản phẩm không được vượt quá 6 MB.")

    content_type = file_storage.mimetype or mimetypes.guess_type(original)[0]
    if content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("Định dạng ảnh không hợp lệ.")

    object_path = f"products/{datetime.now(timezone.utc):%Y/%m}/{secrets.token_hex(16)}.{extension}"
    _supabase_storage_request("POST", object_path, payload, content_type)
    return _public_storage_url(object_path)


# =========================================================
# ROUTE GIAO DIỆN
# =========================================================

@app.get("/")
def home():
    featured_products = (
        Product.query
        .filter_by(is_active=True)
        .order_by(Product.created_at.desc())
        .limit(6)
        .all()
    )
    product_ids = [product.id for product in featured_products]
    delivery_map = {
        item.product_id: item
        for item in ProductDelivery.query.filter(
            ProductDelivery.product_id.in_(product_ids or [-1])
        ).all()
    }
    stock_counts = dict(
        db.session.query(ProductStock.product_id, func.count(ProductStock.id))
        .filter_by(status="available")
        .group_by(ProductStock.product_id)
        .all()
    )
    return render_template(
        "index.html",
        current_user=current_user(),
        products=featured_products,
        delivery_map=delivery_map,
        stock_counts=stock_counts,
    )


@app.get("/auth")
def auth():
    if session.get("user_id"):
        return redirect(
            url_for("profile")
        )

    return render_template("auth.html")


def canonical_product_key(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return value or "product"


def seller_offer_data(product):
    shop = Shop.query.filter_by(seller_id=product.seller_id, status="active").first()
    seller = db.session.get(User, product.seller_id) if product.seller_id else None
    delivery = ProductDelivery.query.filter_by(product_id=product.id).first()
    stock = ProductStock.query.filter_by(product_id=product.id, status="available").count()
    return {
        "product": product, "shop": shop, "seller": seller, "delivery": delivery, "stock": stock,
        "seller_name": shop.name if shop else (seller.username if seller else "Người bán"),
        "rating": float(shop.rating_average or 0) if shop else 0,
        "rating_count": int(shop.rating_count or 0) if shop else 0,
    }


@app.get("/marketplace")
def marketplace():
    products = Product.query.filter_by(is_active=True).order_by(Product.created_at.desc()).all()
    delivery_map = {d.product_id: d for d in ProductDelivery.query.filter(ProductDelivery.product_id.in_([p.id for p in products] or [-1])).all()}
    stock_counts = dict(db.session.query(ProductStock.product_id, func.count(ProductStock.id)).filter_by(status="available").group_by(ProductStock.product_id).all())
    return render_template(
        "marketplace.html",
        current_user=current_user(),
        shops=Shop.query.filter_by(status="active").order_by(Shop.rating_average.desc(), Shop.created_at.desc()).all(),
        products=products,
        delivery_map=delivery_map,
        stock_counts=stock_counts,
    )


@app.get("/product/<int:product_id>")
def product_detail(product_id):
    product = db.session.get(Product, product_id)

    if not product:
        return render_template(
            "product.html",
            product=None,
        ), 404

    delivery = ProductDelivery.query.filter_by(product_id=product.id).first()
    shop = Shop.query.filter_by(seller_id=product.seller_id, status="active").first()
    seller = db.session.get(User, product.seller_id) if product.seller_id else None
    stock_count = ProductStock.query.filter_by(
        product_id=product.id,
        status="available",
    ).count()

    delivery_label = (
        "Giao tự động"
        if delivery and delivery.delivery_type == "automatic"
        else "Xử lý qua ticket"
    )
    seller_name = shop.name if shop else (seller.username if seller else "Người bán")

    key = product.canonical_key or canonical_product_key(product.name)
    candidates = Product.query.filter_by(is_active=True).all()
    matching = [p for p in candidates if (p.canonical_key or canonical_product_key(p.name)) == key]
    offers = sorted((seller_offer_data(p) for p in matching), key=lambda item: (int(item["product"].price), -item["rating"]))

    return render_template(
        "product.html", product=product, current_user=current_user(), delivery=delivery,
        delivery_label=delivery_label, stock_count=stock_count, seller_name=seller_name, offers=offers,
    )


def get_cart_quantities():
    """Trả về {product_id: quantity} và tự chuyển dữ liệu giỏ hàng cũ."""
    raw = session.get("cart_quantities")
    cart = {}

    if isinstance(raw, dict):
        for product_id, quantity in raw.items():
            try:
                pid = int(product_id)
                qty = max(1, min(99, int(quantity)))
            except (TypeError, ValueError):
                continue
            cart[pid] = qty
    else:
        # Tương thích với phiên bản cũ chỉ lưu danh sách ID.
        old_ids = session.get("cart_product_ids", [])
        if isinstance(old_ids, list):
            for value in old_ids:
                try:
                    pid = int(value)
                except (TypeError, ValueError):
                    continue
                cart[pid] = cart.get(pid, 0) + 1

    session["cart_quantities"] = {str(pid): qty for pid, qty in cart.items()}
    session.pop("cart_product_ids", None)
    session.modified = True
    return cart


def save_cart_quantities(cart):
    session["cart_quantities"] = {
        str(int(product_id)): max(1, min(99, int(quantity)))
        for product_id, quantity in cart.items()
        if int(quantity) > 0
    }
    session.modified = True


def get_cart_items():
    cart = get_cart_quantities()
    if not cart:
        return []

    products = Product.query.filter(
        Product.id.in_(list(cart.keys())),
        Product.is_active.is_(True),
    ).all()
    product_map = {product.id: product for product in products}

    items = []
    for product_id, quantity in cart.items():
        product = product_map.get(product_id)
        if not product:
            continue
        items.append({
            "product": product,
            "quantity": quantity,
            "subtotal": int(product.price or 0) * quantity,
        })
    return items


@app.get("/cart")
def cart():
    items = get_cart_items()
    total = sum(item["subtotal"] for item in items)
    return render_template(
        "cart.html",
        current_user=current_user(),
        items=items,
        total=total,
    )


@app.post("/cart/add/<int:product_id>")
def add_to_cart(product_id):
    product = db.session.get(Product, product_id)
    if not product or not product.is_active:
        flash("Sản phẩm không tồn tại hoặc đã ngừng bán.", "error")
        return redirect(url_for("marketplace"))

    cart = get_cart_quantities()
    cart[product_id] = min(99, cart.get(product_id, 0) + 1)
    save_cart_quantities(cart)
    flash(f"Đã thêm {product.name} vào giỏ hàng.", "success")
    return redirect(request.referrer or url_for("marketplace"))


@app.post("/cart/update/<int:product_id>")
def update_cart_quantity(product_id):
    cart = get_cart_quantities()
    if product_id not in cart:
        flash("Sản phẩm không còn trong giỏ hàng.", "error")
        return redirect(url_for("cart"))

    try:
        quantity = int(request.form.get("quantity", "1"))
    except ValueError:
        quantity = 1

    if quantity <= 0:
        cart.pop(product_id, None)
        flash("Đã xóa sản phẩm khỏi giỏ hàng.", "success")
    else:
        cart[product_id] = min(99, quantity)
        flash("Đã cập nhật số lượng.", "success")

    save_cart_quantities(cart)
    return redirect(url_for("cart"))


@app.post("/cart/remove/<int:product_id>")
def remove_from_cart(product_id):
    cart = get_cart_quantities()
    cart.pop(product_id, None)
    save_cart_quantities(cart)
    flash("Đã xóa sản phẩm khỏi giỏ hàng.", "success")
    return redirect(url_for("cart"))


@app.get("/checkout")
@login_required
def checkout():
    items = get_cart_items()
    if not items:
        flash("Giỏ hàng đang trống.", "info")
        return redirect(url_for("cart"))

    user = current_user()
    products = [item["product"] for item in items]
    total = sum(item["subtotal"] for item in items)
    delivery_map = {
        item.product_id: item
        for item in ProductDelivery.query.filter(
            ProductDelivery.product_id.in_([product.id for product in products])
        ).all()
    }
    return render_template(
        "checkout.html",
        current_user=user,
        items=items,
        total=total,
        delivery_map=delivery_map,
    )


@app.get("/checkout/<int:product_id>")
@login_required
def checkout_product(product_id):
    product = db.session.get(Product, product_id)
    if not product or not product.is_active:
        flash("Sản phẩm không tồn tại hoặc đã ngừng bán.", "error")
        return redirect(url_for("marketplace"))
    save_cart_quantities({product.id: 1})
    return redirect(url_for("checkout"))


@app.route("/become-seller", methods=["GET", "POST"])
@login_required
def become_seller():
    user = current_user()
    role = normalized_role(user)

    # Founder/Admin đã có quyền quản trị, Seller đã được duyệt.
    if role in {"founder", "admin"}:
        flash("Tài khoản quản trị không cần gửi đơn đăng ký Seller.", "info")
        return redirect(url_for("admin"))

    if role == "seller":
        return redirect(url_for("seller_dashboard"))

    latest_request = (
        SellerRequest.query
        .filter_by(user_id=user.id)
        .order_by(SellerRequest.created_at.desc())
        .first()
    )
    pending_request = (
        latest_request
        if latest_request and latest_request.status == "pending"
        else None
    )

    if request.method == "POST":
        if pending_request:
            flash("Bạn đã có yêu cầu đang chờ Founder/Admin xét duyệt.", "info")
            return redirect(url_for("become_seller"))

        fields = {
            key: request.form.get(key, "").strip()
            for key in [
                "shop_name",
                "full_name",
                "email",
                "phone",
                "category",
                "description",
            ]
        }

        if not all(fields.values()):
            flash("Vui lòng nhập đầy đủ các trường bắt buộc.", "error")
            return render_template(
                "become-seller.html",
                current_user=user,
                existing_request=None,
                latest_request=latest_request,
            ), 400

        seller_request = SellerRequest(user_id=user.id, **fields)
        db.session.add(seller_request)

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Không thể lưu yêu cầu Seller")
            flash("Không thể lưu yêu cầu lúc này. Vui lòng thử lại.", "error")
            return redirect(url_for("become_seller"))

        webhook_ok, webhook_message = send_discord_webhook(
            "🛍️ YÊU CẦU TRỞ THÀNH SELLER",
            (
                f"**Mã yêu cầu:** #{seller_request.id}\n"
                f"**Tài khoản:** {user.username} (ID {user.id})\n"
                f"**Email tài khoản:** {user.email}\n"
                f"**Email đăng ký:** {fields['email']}\n"
                f"**Tên gian hàng:** {fields['shop_name']}\n"
                f"**Người đại diện:** {fields['full_name']}\n"
                f"**Danh mục:** {fields['category']}\n"
                f"**Số điện thoại:** {fields['phone']}\n"
                f"**Mô tả:** {fields['description']}"
            ),
            0xF1C40F,
        )

        if webhook_ok:
            flash(
                "Đã gửi yêu cầu thành công. Bạn vui lòng chờ Founder/Admin xét duyệt.",
                "success",
            )
        else:
            flash(
                "Yêu cầu đã được lưu và đang chờ duyệt, nhưng Discord webhook chưa gửi được. "
                f"Lỗi: {webhook_message}",
                "warning",
            )

        return redirect(url_for("become_seller"))

    return render_template(
        "become-seller.html",
        current_user=user,
        existing_request=pending_request,
        latest_request=latest_request,
    )


@app.get("/orders")
@login_required
def orders():
    user = current_user()
    user_orders = MarketplaceOrder.query.filter_by(buyer_id=user.id).order_by(MarketplaceOrder.created_at.desc()).all()
    product_map = {p.id: p for p in Product.query.filter(Product.id.in_([o.product_id for o in user_orders] or [-1])).all()}
    processing_orders = sum(1 for o in user_orders if o.status not in {"completed", "cancelled", "refunded"})
    completed_orders = sum(1 for o in user_orders if o.status == "completed")
    total_spent = sum(int(o.amount or 0) for o in user_orders)
    return render_template(
        "orders.html", current_user=user, orders=user_orders, product_map=product_map,
        total_orders=len(user_orders), processing_orders=processing_orders,
        completed_orders=completed_orders, total_spent=total_spent,
    )


@app.get("/wallet")
def wallet():
    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("auth", mode="login"))

    user = db.session.get(User, user_id)

    if not user:
        session.clear()
        return redirect(url_for("auth", mode="login"))

    # Nạp tiền đã được duyệt thực tế. Không dùng số dư hiện tại để suy ra
    # tổng đã nạp vì số dư còn thay đổi sau mua hàng và hoàn tiền.
    completed_deposits = (
        DepositRequest.query
        .filter_by(user_id=user.id, status="completed", wallet_credited=True)
        .order_by(DepositRequest.processed_at.desc(), DepositRequest.created_at.desc())
        .all()
    )
    total_deposited = sum(int(item.amount or 0) for item in completed_deposits)

    paid_orders = (
        MarketplaceOrder.query
        .filter(MarketplaceOrder.buyer_id == user.id)
        .filter(~MarketplaceOrder.status.in_(["cancelled", "refunded"]))
        .order_by(MarketplaceOrder.created_at.desc())
        .all()
    )
    total_spent = sum(int(item.amount or 0) for item in paid_orders)

    transactions = []
    for item in completed_deposits:
        transactions.append(SimpleNamespace(
            amount=int(item.amount or 0),
            description="Nạp tiền vào ví",
            reference_code=item.code,
            created_at=item.processed_at or item.created_at,
        ))
    for order in paid_orders:
        product = db.session.get(Product, order.product_id)
        transactions.append(SimpleNamespace(
            amount=-int(order.amount or 0),
            description=f"Mua {product.name if product else 'sản phẩm'}",
            reference_code=order.reference,
            created_at=order.created_at,
        ))
    transactions.sort(
        key=lambda tx: (
            to_vietnam_time(tx.created_at) or datetime.min.replace(tzinfo=VN_TIMEZONE)
        ),
        reverse=True,
    )

    return render_template(
        "wallet.html",
        user=user,
        current_user=user,
        transactions=transactions,
        total_deposited=total_deposited,
        total_spent=total_spent,
    )


@app.get("/profile")
@login_required
def profile():
    user = current_user()

    if not user:
        session.clear()

        return redirect(
            url_for(
                "auth",
                mode="login",
            )
        )

    return render_template(
        "profile.html",
        user=user,
        current_user=user,
    )


@app.route("/seller-dashboard", methods=["GET", "POST"])
@login_required
def seller_dashboard():
    user = current_user()
    if normalized_role(user) not in {"seller", "admin", "founder"}:
        flash("Bạn chưa được duyệt trở thành Seller.", "error")
        return redirect(url_for("become_seller"))
    release_due_holds()
    shop = Shop.query.filter_by(seller_id=user.id).first()
    wallet = SellerWallet.query.filter_by(seller_id=user.id).first()
    if not wallet:
        wallet = SellerWallet(seller_id=user.id)
        db.session.add(wallet); db.session.commit()
    if request.method == "POST":
        name=request.form.get("name", "").strip(); description=request.form.get("description", "").strip(); support=request.form.get("support_info", "").strip(); avatar=request.form.get("avatar_url", "").strip() or None
        if not name or not description or not support:
            flash("Tên, mô tả và thông tin hỗ trợ là bắt buộc.", "error")
        elif shop:
            shop.name=name; shop.description=description; shop.support_info=support; shop.avatar_url=avatar
            db.session.commit(); flash("Đã cập nhật gian hàng.", "success")
        else:
            slug=slugify_shop(name); base=slug; i=2
            while Shop.query.filter_by(slug=slug).first(): slug=f"{base}-{i}"; i+=1
            shop=Shop(seller_id=user.id, name=name, slug=slug, description=description, support_info=support, avatar_url=avatar)
            db.session.add(shop); db.session.commit(); flash("Đã tạo gian hàng.", "success")
        return redirect(url_for("seller_dashboard"))
    holds=SellerHold.query.filter_by(seller_id=user.id).order_by(SellerHold.held_at.desc()).all()
    withdrawals=WithdrawalRequest.query.filter_by(seller_id=user.id).order_by(WithdrawalRequest.created_at.desc()).all()
    products=Product.query.filter_by(seller_id=user.id).order_by(Product.created_at.desc()).all()
    delivery_map={d.product_id:d for d in ProductDelivery.query.filter(ProductDelivery.product_id.in_([p.id for p in products] or [-1])).all()}
    stock_counts=dict(db.session.query(ProductStock.product_id,func.count(ProductStock.id)).filter_by(status="available").group_by(ProductStock.product_id).all())
    available_stock_map = {}
    for stock in ProductStock.query.filter(
        ProductStock.product_id.in_([p.id for p in products] or [-1]),
        ProductStock.status == "available",
    ).order_by(ProductStock.id.asc()).all():
        available_stock_map.setdefault(stock.product_id, []).append(stock.content)
    seller_orders=MarketplaceOrder.query.filter_by(seller_id=user.id).order_by(MarketplaceOrder.created_at.desc()).all()

    # Bù dữ liệu cho các đơn đã thanh toán từ phiên bản cũ: Seller vẫn thấy tiền
    # đang giữ dù Buyer chưa bấm xác nhận hoặc chưa đánh giá.
    existing_hold_refs = {h.order_reference for h in holds}
    added_legacy_holds = False
    for order in seller_orders:
        if order.reference in existing_hold_refs or order.status in {"cancelled", "refunded"}:
            continue
        fee = round(int(order.amount) * 0.10)
        seller_amount = int(order.amount) - fee
        wallet.pending_balance = int(wallet.pending_balance or 0) + seller_amount
        held_at = order.created_at or utc_now()
        db.session.add(SellerHold(
            seller_id=user.id,
            order_reference=order.reference,
            gross_amount=order.amount,
            platform_fee=fee,
            seller_amount=seller_amount,
            status="holding",
            held_at=held_at,
            release_at=held_at + timedelta(days=7),
        ))
        existing_hold_refs.add(order.reference)
        added_legacy_holds = True
    if added_legacy_holds:
        db.session.commit()
        release_due_holds()
        wallet = SellerWallet.query.filter_by(seller_id=user.id).first()
        holds = SellerHold.query.filter_by(seller_id=user.id).order_by(SellerHold.held_at.desc()).all()

    return render_template("seller-dashboard.html", current_user=user, user=user, shop=shop, wallet=wallet, holds=holds, withdrawals=withdrawals, products=products, delivery_map=delivery_map, stock_counts=stock_counts, available_stock_map=available_stock_map, seller_orders=seller_orders)


@app.post("/seller/products/create")
@login_required
def seller_create_product():
    user = current_user()
    if normalized_role(user) not in {"seller", "admin", "founder"}:
        flash("Bạn chưa có quyền Seller.", "error")
        return redirect(url_for("profile"))
    if not Shop.query.filter_by(seller_id=user.id).first():
        flash("Hãy tạo gian hàng trước khi đăng sản phẩm.", "error")
        return redirect(url_for("seller_dashboard", tab="shop"))
    name=request.form.get("name","").strip(); description=request.form.get("description","").strip()
    category=request.form.get("category","Dịch vụ").strip() or "Dịch vụ"
    allowed_categories={"Gift Card","Tài khoản","Dịch vụ","Game","Discord","Streaming","Phần mềm","AI","Khác"}
    if category not in allowed_categories: category="Khác"
    delivery_type=request.form.get("delivery_type","").strip().lower(); instructions=request.form.get("instructions","").strip() or None
    image_url = None
    try:
        uploaded_image_url = save_product_image(request.files.get("product_image"))
        if uploaded_image_url:
            image_url = uploaded_image_url
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("seller_dashboard", tab="products"))
    try: price=int(request.form.get("price","0"))
    except ValueError: price=0
    if not name or not description or price < 1000 or delivery_type not in {"automatic","manual"}:
        flash("Thông tin sản phẩm hoặc cách giao hàng không hợp lệ.", "error")
        return redirect(url_for("seller_dashboard", tab="products"))
    product=Product(name=name,description=description,category=category,image_url=image_url,price=price,seller_id=user.id,is_active=True,canonical_key=canonical_product_key(name))
    db.session.add(product); db.session.flush()
    db.session.add(ProductDelivery(product_id=product.id,delivery_type=delivery_type,instructions=instructions))
    if delivery_type == "automatic":
        lines=[x.strip() for x in request.form.get("stock_content","").splitlines() if x.strip()]
        if not lines:
            db.session.rollback(); flash("Giao tự động cần ít nhất một dòng hàng trong kho.","error"); return redirect(url_for("seller_dashboard", tab="products"))
        for line in lines:
            db.session.add(ProductStock(product_id=product.id,content=line))
    else:
        try:
            manual_quantity = int(request.form.get("manual_stock_quantity", "0"))
        except (TypeError, ValueError):
            manual_quantity = 0
        if manual_quantity < 1 or manual_quantity > 100000:
            db.session.rollback(); flash("Hàng giao qua ticket cần số lượng nhận đơn từ 1 đến 100.000.", "error"); return redirect(url_for("seller_dashboard", tab="products"))
        for _ in range(manual_quantity):
            db.session.add(ProductStock(product_id=product.id, content="__MANUAL_TICKET_SLOT__"))
    db.session.commit(); flash("Đã đăng sản phẩm.","success")
    return redirect(url_for("seller_dashboard", tab="products"))


@app.post("/seller/products/<int:product_id>/edit")
@login_required
def seller_edit_product(product_id):
    user = current_user()
    product = db.session.get(Product, product_id)
    if not product or product.seller_id != user.id:
        flash("Không tìm thấy sản phẩm hoặc bạn không có quyền chỉnh sửa.", "error")
        return redirect(url_for("seller_dashboard", tab="products"))

    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    category = request.form.get("category", "Dịch vụ").strip() or "Dịch vụ"
    allowed_categories = {"Gift Card", "Tài khoản", "Dịch vụ", "Game", "Discord", "Streaming", "Phần mềm", "AI", "Khác"}
    if category not in allowed_categories:
        category = "Khác"
    try:
        price = int(request.form.get("price", "0"))
    except ValueError:
        price = 0
    if not name or not description or price < 1000:
        flash("Tên, mô tả hoặc giá bán không hợp lệ.", "error")
        return redirect(url_for("seller_dashboard", tab="products"))

    old_image_url = product.image_url
    image_url = old_image_url
    try:
        uploaded_image_url = save_product_image(request.files.get("product_image"))
        if uploaded_image_url:
            image_url = uploaded_image_url
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("seller_dashboard", tab="products"))

    delivery = ProductDelivery.query.filter_by(product_id=product.id).first()
    delivery_type = request.form.get("delivery_type", delivery.delivery_type if delivery else "manual").strip().lower()
    if delivery_type not in {"automatic", "manual"}:
        delivery_type = "manual"
    instructions = request.form.get("instructions", "").strip() or None

    product.name = name
    product.description = description
    product.category = category
    product.price = price
    product.image_url = image_url
    product.is_active = request.form.get("is_active") == "on"
    if not delivery:
        delivery = ProductDelivery(product_id=product.id)
        db.session.add(delivery)
    delivery.delivery_type = delivery_type
    delivery.instructions = instructions

    edited_stock = [line.strip() for line in request.form.get("stock_content", "").splitlines() if line.strip()]
    if delivery_type == "automatic":
        # Form chỉnh sửa là nguồn kho hiện tại: thay toàn bộ các dòng còn available,
        # nhưng giữ nguyên lịch sử hàng đã bán/đã gắn với đơn cũ.
        ProductStock.query.filter_by(
            product_id=product.id,
            status="available",
        ).delete(synchronize_session=False)
        for line in edited_stock:
            db.session.add(ProductStock(product_id=product.id, content=line))
    else:
        try:
            target_quantity = int(request.form.get("manual_stock_target", "0"))
        except (TypeError, ValueError):
            target_quantity = -1
        if target_quantity < 0 or target_quantity > 100000:
            flash("Stock qua ticket phải từ 0 đến 100.000.", "error")
            return redirect(url_for("seller_dashboard", tab="products"))

        # Chuyển sang giao ticket thì xóa mọi hàng available cũ, sau đó tạo đúng
        # số slot seller nhập. Các hàng đã bán vẫn được giữ để bảo toàn lịch sử.
        ProductStock.query.filter_by(
            product_id=product.id,
            status="available",
        ).delete(synchronize_session=False)
        for _ in range(target_quantity):
            db.session.add(ProductStock(product_id=product.id, content="__MANUAL_TICKET_SLOT__"))

    db.session.commit()
    if image_url != old_image_url and old_image_url:
        delete_product_image(old_image_url)
    flash("Đã cập nhật sản phẩm.", "success")
    return redirect(url_for("seller_dashboard", tab="products"))


@app.post("/seller/products/<int:product_id>/delete")
@login_required
def seller_delete_product(product_id):
    user = current_user()
    product = db.session.get(Product, product_id)
    if not product or product.seller_id != user.id:
        flash("Không tìm thấy sản phẩm hoặc bạn không có quyền xóa.", "error")
        return redirect(url_for("seller_dashboard", tab="products"))

    # Xóa mềm để không làm hỏng đơn hàng và lịch sử giao dịch cũ.
    product.is_active = False
    db.session.commit()
    flash("Đã gỡ sản phẩm khỏi marketplace. Lịch sử đơn hàng cũ vẫn được giữ lại.", "success")
    return redirect(url_for("seller_dashboard", tab="products"))


@app.post("/seller/products/<int:product_id>/stock")
@login_required
def seller_add_stock(product_id):
    user=current_user(); product=db.session.get(Product,product_id)
    if not product or product.seller_id != user.id: flash("Không tìm thấy sản phẩm.","error"); return redirect(url_for("seller_dashboard", tab="products"))
    delivery=ProductDelivery.query.filter_by(product_id=product.id).first()
    if not delivery:
        flash("Sản phẩm chưa được cấu hình cách giao hàng.", "error")
        return redirect(url_for("seller_dashboard", tab="products"))
    if delivery.delivery_type == "automatic":
        lines=[x.strip() for x in request.form.get("stock_content","").splitlines() if x.strip()]
        for line in lines:
            db.session.add(ProductStock(product_id=product.id,content=line))
        added = len(lines)
    else:
        try:
            added = int(request.form.get("manual_stock_quantity", "0"))
        except (TypeError, ValueError):
            added = 0
        if added < 1 or added > 100000:
            flash("Số lượng cần thêm phải từ 1 đến 100.000.", "error")
            return redirect(url_for("seller_dashboard", tab="products"))
        for _ in range(added):
            db.session.add(ProductStock(product_id=product.id, content="__MANUAL_TICKET_SLOT__"))
    db.session.commit(); flash(f"Đã thêm {added} hàng vào kho.","success")
    return redirect(url_for("seller_dashboard", tab="products"))


@app.post("/products/<int:product_id>/buy")
@login_required
def buy_product(product_id):
    return redirect(url_for("checkout_product", product_id=product_id))


@app.post("/checkout/complete")
@login_required
def complete_checkout():
    buyer = current_user()
    items = get_cart_items()
    if not items:
        flash("Giỏ hàng đang trống.", "error")
        return redirect(url_for("cart"))

    if request.form.get("payment_method") != "wallet":
        flash("Phương thức thanh toán không hợp lệ.", "error")
        return redirect(url_for("checkout"))

    products = [item["product"] for item in items]
    if any(product.seller_id == buyer.id for product in products):
        flash("Bạn không thể mua sản phẩm của chính mình.", "error")
        return redirect(url_for("checkout"))

    total = sum(item["subtotal"] for item in items)
    if int(buyer.balance or 0) < total:
        flash("Số dư ví không đủ để thanh toán đơn hàng.", "error")
        return redirect(url_for("checkout"))

    deliveries = {
        delivery.product_id: delivery
        for delivery in ProductDelivery.query.filter(
            ProductDelivery.product_id.in_([product.id for product in products])
        ).all()
    }
    created_orders = []
    seller_wallets = {}

    try:
        for cart_item in items:
            product = cart_item["product"]
            quantity = cart_item["quantity"]
            delivery = deliveries.get(product.id)
            if not delivery:
                raise ValueError(f"Sản phẩm {product.name} chưa được cấu hình cách giao hàng.")

            available_stocks = (
                ProductStock.query
                .filter_by(product_id=product.id, status="available")
                .with_for_update()
                .limit(quantity)
                .all()
            )
            if len(available_stocks) < quantity:
                raise ValueError(
                    f"Sản phẩm {product.name} chỉ còn {len(available_stocks)} hàng trong kho, "
                    f"không đủ số lượng {quantity}."
                )

            for index in range(quantity):
                stock = available_stocks[index]
                order = MarketplaceOrder(
                    reference="KY" + secrets.token_hex(5).upper(),
                    buyer_id=buyer.id,
                    seller_id=product.seller_id,
                    product_id=product.id,
                    amount=product.price,
                    delivery_type=delivery.delivery_type,
                    status="delivered" if delivery.delivery_type == "automatic" else "processing",
                )
                db.session.add(order)
                db.session.flush()

                stock.status = "sold"
                stock.order_id = order.id
                stock.sold_at = utc_now()
                if delivery.delivery_type == "automatic":
                    order.delivered_content = stock.content
                    order.delivered_at = utc_now()
                else:
                    db.session.add(TicketMessage(
                        order_id=order.id,
                        sender_id=buyer.id,
                        content="Ticket đã được tạo. Buyer và Seller có thể trao đổi tại đây.",
                    ))
                fee = round(int(order.amount) * 0.10)
                seller_amount = int(order.amount) - fee
                wallet = seller_wallets.get(order.seller_id)
                if wallet is None:
                    wallet = SellerWallet.query.filter_by(seller_id=order.seller_id).with_for_update().first()
                    if not wallet:
                        wallet = SellerWallet(seller_id=order.seller_id)
                        db.session.add(wallet)
                        db.session.flush()
                    seller_wallets[order.seller_id] = wallet
                wallet.pending_balance = int(wallet.pending_balance or 0) + seller_amount
                db.session.add(SellerHold(
                    seller_id=order.seller_id,
                    order_reference=order.reference,
                    gross_amount=order.amount,
                    platform_fee=fee,
                    seller_amount=seller_amount,
                    status="holding",
                    held_at=utc_now(),
                    release_at=utc_now() + timedelta(days=7),
                ))
                created_orders.append(order)

        buyer.balance -= total
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("cart"))
    except Exception:
        db.session.rollback()
        app.logger.exception("Không thể hoàn tất thanh toán")
        flash("Không thể hoàn tất thanh toán lúc này. Vui lòng thử lại.", "error")
        return redirect(url_for("checkout"))

    save_cart_quantities({})

    try:
        send_order_confirmation_email(
            email=buyer.email,
            username=buyer.username,
            orders=created_orders,
            products={product.id: product for product in products},
            total=total,
            orders_url=url_for("orders", _external=True),
        )
    except Exception:
        app.logger.exception(
            "Đơn hàng đã tạo nhưng không gửi được email xác nhận cho %s",
            buyer.email,
        )

    order_ids = [order.id for order in created_orders]
    return render_template(
        "checkout-success.html",
        current_user=buyer,
        orders=created_orders,
        products={product.id: product for product in products},
        total=total,
        order_ids=order_ids,
    )


@app.route("/orders/<int:order_id>/ticket",methods=["GET","POST"])
@login_required
def order_ticket(order_id):
    user=current_user(); order=MarketplaceOrder.query.get_or_404(order_id)
    if user.id not in {order.buyer_id,order.seller_id} and normalized_role(user) not in {"admin","founder"}:
        flash("Bạn không có quyền xem ticket này.","error"); return redirect(url_for("orders"))
    if request.method == "POST":
        content=request.form.get("content","").strip()
        if content: db.session.add(TicketMessage(order_id=order.id,sender_id=user.id,content=content)); db.session.commit()
        return redirect(url_for("order_ticket",order_id=order.id))
    messages=TicketMessage.query.filter_by(order_id=order.id).order_by(TicketMessage.created_at.asc()).all()
    users={u.id:u for u in User.query.filter(User.id.in_([order.buyer_id,order.seller_id]+[m.sender_id for m in messages])).all()}
    product=db.session.get(Product,order.product_id)
    return render_template("order-ticket.html",current_user=user,order=order,product=product,messages=messages,users=users)


@app.post("/orders/<int:order_id>/mark-delivered")
@login_required
def mark_order_delivered(order_id):
    user=current_user(); order=MarketplaceOrder.query.get_or_404(order_id)
    if user.id != order.seller_id: flash("Chỉ Seller của đơn mới được xác nhận giao hàng.","error"); return redirect(url_for("order_ticket",order_id=order.id))
    note=request.form.get("delivery_note","").strip()
    if note: db.session.add(TicketMessage(order_id=order.id,sender_id=user.id,content="📦 Thông tin giao hàng: "+note))
    order.status="delivered"; order.delivered_at=utc_now(); db.session.commit(); flash("Đã đánh dấu giao hàng.","success")
    return redirect(url_for("order_ticket",order_id=order.id))


@app.post("/orders/<int:order_id>/confirm")
@login_required
def confirm_order(order_id):
    buyer=current_user(); order=MarketplaceOrder.query.get_or_404(order_id)
    if buyer.id != order.buyer_id or order.status != "delivered": flash("Không thể xác nhận đơn này.","error"); return redirect(url_for("order_ticket",order_id=order.id))
    order.status="completed"
    order.completed_at=utc_now()
    db.session.commit()
    flash("Đã xác nhận nhận hàng. Khoản tiền Seller đã được ghi nhận từ lúc thanh toán và vẫn tiếp tục được giữ đủ 7 ngày.", "success")
    return redirect(url_for("review_order", order_id=order.id))


@app.route("/orders/<int:order_id>/review", methods=["GET", "POST"])
@login_required
def review_order(order_id):
    buyer = current_user()
    order = MarketplaceOrder.query.get_or_404(order_id)

    if buyer.id != order.buyer_id:
        flash("Bạn không có quyền đánh giá đơn hàng này.", "error")
        return redirect(url_for("orders"))

    if order.status != "completed":
        flash("Chỉ có thể đánh giá sau khi đã nhận hàng.", "error")
        return redirect(url_for("order_ticket", order_id=order.id))

    existing_review = Review.query.filter_by(order_reference=order.reference).first()
    if existing_review:
        flash("Đơn hàng này đã được đánh giá rồi.", "info")
        return redirect(url_for("order_ticket", order_id=order.id))

    shop = Shop.query.filter_by(seller_id=order.seller_id).first()
    product = db.session.get(Product, order.product_id)

    if not shop:
        flash("Không tìm thấy gian hàng để đánh giá.", "error")
        return redirect(url_for("order_ticket", order_id=order.id))

    if request.method == "POST":
        try:
            rating = int(request.form.get("rating", "0"))
        except ValueError:
            rating = 0

        content = request.form.get("content", "").strip()

        if rating not in range(1, 6):
            flash("Vui lòng chọn từ 1 đến 5 sao.", "error")
            return render_template(
                "review-order.html",
                current_user=buyer,
                order=order,
                shop=shop,
                product=product,
            )

        if len(content) < 3:
            flash("Nội dung đánh giá cần ít nhất 3 ký tự.", "error")
            return render_template(
                "review-order.html",
                current_user=buyer,
                order=order,
                shop=shop,
                product=product,
            )

        review = Review(
            order_reference=order.reference,
            buyer_id=buyer.id,
            shop_id=shop.id,
            rating=rating,
            content=content,
        )
        db.session.add(review)
        db.session.flush()

        avg, count = db.session.query(
            func.avg(Review.rating),
            func.count(Review.id),
        ).filter_by(shop_id=shop.id).one()

        shop.rating_average = float(avg or 0)
        shop.rating_count = int(count or 0)
        db.session.commit()

        flash("Cảm ơn bạn đã đánh giá gian hàng.", "success")
        return redirect(url_for("order_ticket", order_id=order.id))

    return render_template(
        "review-order.html",
        current_user=buyer,
        order=order,
        shop=shop,
        product=product,
    )


@app.post("/seller/withdraw")
@login_required
def seller_withdraw():
    user=current_user()
    if normalized_role(user) not in {"seller", "admin", "founder"}: return redirect(url_for("profile"))
    release_due_holds()
    wallet=SellerWallet.query.filter_by(seller_id=user.id).first()
    try: amount=int(request.form.get("amount", "0"))
    except ValueError: amount=0
    if not wallet or amount < 10000 or amount > int(wallet.available_balance or 0):
        flash("Số tiền rút không hợp lệ hoặc vượt số dư khả dụng.", "error"); return redirect(url_for("seller_dashboard"))
    bank=request.form.get("bank_name", "").strip(); number=request.form.get("account_number", "").strip(); name=request.form.get("account_name", "").strip()
    if not bank or not number or not name:
        flash("Vui lòng nhập đầy đủ thông tin ngân hàng.", "error"); return redirect(url_for("seller_dashboard"))
    wallet.available_balance -= amount; wallet.withdrawal_hold += amount
    wr=WithdrawalRequest(seller_id=user.id, amount=amount, bank_name=bank, account_number=number, account_name=name)
    db.session.add(wr); db.session.commit()
    send_discord_webhook("💸 Yêu cầu rút tiền", f"**Seller:** {user.username}\n**Số tiền:** {amount:,.0f}đ\n**Ngân hàng:** {bank}\n**STK:** {number}\n**Chủ TK:** {name}", 0xE67E22)
    flash("Đã gửi yêu cầu rút tiền.", "success"); return redirect(url_for("seller_dashboard"))


@app.get("/admin")
@admin_required
def admin():
    user=current_user(); release_due_holds()
    from deposit import init_deposit as _unused
    pending_sellers=SellerRequest.query.filter_by(status="pending").order_by(SellerRequest.created_at.desc()).all()
    pending_withdrawals=WithdrawalRequest.query.filter_by(status="pending").order_by(WithdrawalRequest.created_at.desc()).all()
    pending_reports=Report.query.filter_by(status="pending").order_by(Report.created_at.desc()).all()
    platform_revenue=db.session.query(func.coalesce(func.sum(SellerHold.platform_fee),0)).filter(SellerHold.status.in_(["holding","released"])).scalar() or 0
    gross_sales=db.session.query(func.coalesce(func.sum(SellerHold.gross_amount),0)).scalar() or 0
    held_money=db.session.query(func.coalesce(func.sum(SellerHold.seller_amount),0)).filter_by(status="holding").scalar() or 0
    return render_template("admin.html", user=user, current_user=user, is_founder=is_founder(user), pending_sellers=pending_sellers, pending_withdrawals=pending_withdrawals, pending_reports=pending_reports, platform_revenue=platform_revenue, gross_sales=gross_sales, held_money=held_money, seller_count=User.query.filter(func.lower(User.role)=="seller").count())


@app.post("/admin/seller-requests/<int:req_id>/<action>")
@admin_required
def process_seller_request(req_id, action):
    req = SellerRequest.query.get_or_404(req_id)
    admin = current_user()

    if req.status != "pending" or action not in {"approve", "reject"}:
        flash("Yêu cầu không hợp lệ hoặc đã được xử lý.", "error")
        return redirect(url_for("admin"))

    target = db.session.get(User, req.user_id)
    if not target:
        flash("Không tìm thấy tài khoản gửi yêu cầu.", "error")
        return redirect(url_for("admin"))

    note = request.form.get("note", "").strip() or None
    req.status = "approved" if action == "approve" else "rejected"
    req.processed_at = utc_now()
    req.processed_by = admin.id
    req.admin_note = note

    if action == "approve":
        target.role = "seller"
        target.seller_welcome_pending = True
        if not SellerWallet.query.filter_by(seller_id=target.id).first():
            db.session.add(SellerWallet(seller_id=target.id))

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Không thể xử lý yêu cầu Seller")
        flash("Không thể xử lý yêu cầu Seller.", "error")
        return redirect(url_for("admin"))

    email_ok = True
    try:
        send_seller_status_email(
            email=target.email,
            username=target.username,
            shop_name=req.shop_name,
            approved=(action == "approve"),
            note=note,
        )
    except Exception:
        email_ok = False
        app.logger.exception("Không gửi được email kết quả Seller tới %s", target.email)

    if action == "approve":
        send_discord_webhook(
            "✅ ĐÃ DUYỆT SELLER",
            f"**Tài khoản:** {target.username}\n**Gian hàng:** {req.shop_name}\n**Người duyệt:** {admin.username}",
            0x2ECC71,
        )
        message = "Đã duyệt Seller và cấp quyền tạo gian hàng."
    else:
        send_discord_webhook(
            "❌ ĐÃ TỪ CHỐI YÊU CẦU SELLER",
            f"**Tài khoản:** {target.username}\n**Gian hàng:** {req.shop_name}\n**Người xử lý:** {admin.username}\n**Lý do:** {note or 'Không ghi lý do'}",
            0xE74C3C,
        )
        message = "Đã từ chối yêu cầu Seller."

    if not email_ok:
        message += " Tuy nhiên email thông báo chưa gửi được; hãy kiểm tra Resend."

    flash(message, "success" if email_ok else "warning")
    return redirect(url_for("admin"))


@app.post("/admin/withdrawals/<int:req_id>/<action>")
@admin_required
def process_withdrawal(req_id, action):
    wr=WithdrawalRequest.query.get_or_404(req_id); admin=current_user()
    if wr.status != "pending" or action not in {"approve","reject"}:
        flash("Yêu cầu không hợp lệ hoặc đã xử lý.", "error"); return redirect(url_for("admin"))
    wallet=SellerWallet.query.filter_by(seller_id=wr.seller_id).first()
    if action=="approve": wr.status="completed"; wallet.withdrawal_hold=max(0,wallet.withdrawal_hold-wr.amount)
    else: wr.status="rejected"; wallet.withdrawal_hold=max(0,wallet.withdrawal_hold-wr.amount); wallet.available_balance += wr.amount
    wr.admin_note=request.form.get("note", "").strip() or None; wr.processed_at=utc_now(); wr.processed_by=admin.id
    db.session.commit(); flash("Đã xử lý yêu cầu rút tiền.", "success"); return redirect(url_for("admin"))


@app.post("/shops/<int:shop_id>/review")
@login_required
def create_review(shop_id):
    user=current_user(); shop=Shop.query.get_or_404(shop_id)
    try: rating=int(request.form.get("rating", "0"))
    except ValueError: rating=0
    order_ref=request.form.get("order_reference", "").strip(); content=request.form.get("content", "").strip()
    if rating not in range(1,6) or not order_ref or not content or shop.seller_id==user.id:
        flash("Thông tin đánh giá không hợp lệ.", "error"); return redirect(url_for("marketplace"))
    if Review.query.filter_by(order_reference=order_ref).first(): flash("Đơn này đã được đánh giá.", "error"); return redirect(url_for("marketplace"))
    db.session.add(Review(order_reference=order_ref,buyer_id=user.id,shop_id=shop.id,rating=rating,content=content)); db.session.flush()
    avg,count=db.session.query(func.avg(Review.rating),func.count(Review.id)).filter_by(shop_id=shop.id).one(); shop.rating_average=float(avg or 0); shop.rating_count=int(count or 0); db.session.commit()
    flash("Cảm ơn bạn đã đánh giá.", "success"); return redirect(url_for("marketplace"))


@app.post("/shops/<int:shop_id>/report")
@login_required
def create_report(shop_id):
    user=current_user(); shop=Shop.query.get_or_404(shop_id)
    order_ref=request.form.get("order_reference", "").strip(); reason=request.form.get("reason", "").strip(); description=request.form.get("description", "").strip()
    if not order_ref or not reason or not description or shop.seller_id==user.id:
        flash("Thông tin báo cáo không hợp lệ.", "error"); return redirect(url_for("marketplace"))
    db.session.add(Report(reporter_id=user.id,shop_id=shop.id,order_reference=order_ref,reason=reason,description=description)); db.session.commit()
    send_discord_webhook("🚨 Báo cáo gian hàng",f"**Người báo cáo:** {user.username}\n**Shop:** {shop.name}\n**Đơn:** {order_ref}\n**Lý do:** {reason}\n**Chi tiết:** {description}",0xE74C3C)
    flash("Đã gửi báo cáo đến quản trị viên.", "success"); return redirect(url_for("marketplace"))


@app.get("/admin/staff")
@founder_required
def admin_staff():
    user = current_user()

    users = User.query.order_by(
        User.created_at.asc(),
        User.id.asc(),
    ).all()

    founder_count = User.query.filter(
        func.lower(User.role) == "founder"
    ).count()

    return render_template(
        "admin_staff.html",
        user=user,
        current_user=user,
        users=users,
        founder_count=founder_count,
    )


@app.post("/admin/staff/<int:user_id>/role")
@founder_required
def update_staff_role(user_id):
    founder = current_user()
    target = db.session.get(User, user_id)

    if not target:
        flash("Không tìm thấy tài khoản.", "error")
        return redirect(url_for("admin_staff"))

    new_role = str(
        request.form.get("role", "")
    ).strip().lower()

    allowed_roles = {
        "founder",
        "admin",
        "support",
        "seller",
        "buyer",
    }

    if new_role not in allowed_roles:
        flash("Quyền được chọn không hợp lệ.", "error")
        return redirect(url_for("admin_staff"))

    old_role = normalized_role(target)

    # Không cho hạ quyền Founder cuối cùng để tránh khóa toàn bộ trang quản trị.
    if old_role == "founder" and new_role != "founder":
        founder_count = User.query.filter(
            func.lower(User.role) == "founder"
        ).count()

        if founder_count <= 1:
            flash(
                "Không thể hạ quyền Founder cuối cùng của hệ thống.",
                "error",
            )
            return redirect(url_for("admin_staff"))

    target.role = new_role
    target.updated_at = datetime.now(timezone.utc)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Không thể cập nhật quyền tài khoản")
        flash("Không thể cập nhật quyền tài khoản.", "error")
        return redirect(url_for("admin_staff"))

    if target.id == founder.id:
        session["role"] = new_role

    flash(
        f"Đã đổi quyền của {target.username} thành {new_role.upper()}.",
        "success",
    )

    return redirect(url_for("admin_staff"))

@app.post("/login")
def login():
    data = get_json_data()

    account = str(
        data.get("username", "")
    ).strip()

    password = str(
        data.get("password", "")
    )

    remember = bool(data.get("remember", False))

    if not account or not password:
        return json_error(
            "Vui lòng nhập đầy đủ tên người dùng hoặc Gmail và mật khẩu."
        )

    account_lower = account.lower()

    user = User.query.filter(
        db.or_(
            func.lower(User.username) == account_lower,
            func.lower(User.email) == account_lower,
        )
    ).first()

    if not user:
        return json_error(
            "Tên người dùng, Gmail hoặc mật khẩu không đúng.",
            401,
        )

    if not user.is_active:
        return json_error(
            "Tài khoản này đã bị khóa.",
            403,
        )

    if not user.check_password(password):
        return json_error(
            "Tên người dùng, Gmail hoặc mật khẩu không đúng.",
            401,
        )

    session.clear()
    session.permanent = remember
    session["user_id"] = user.id
    session["username"] = user.username
    session["role"] = user.role

    return json_success(
        "Đăng nhập thành công.",
        redirect_url=url_for("profile"),
    )
@app.get("/health")
def health():
    return {
        "status": "ok",
        "database": "connected",
    }

@app.context_processor
def inject_current_user():
    user = None
    seller_request_status = None
    user_id = session.get("user_id")

    if user_id:
        user = db.session.get(User, user_id)
        if user and normalized_role(user) == "buyer":
            latest_request = (
                SellerRequest.query
                .filter_by(user_id=user.id)
                .order_by(SellerRequest.created_at.desc())
                .first()
            )
            if latest_request:
                seller_request_status = latest_request.status

    cart_quantities = get_cart_quantities()
    return {
        "current_user": user,
        "seller_request_status": seller_request_status,
        "cart_count": sum(cart_quantities.values()),
        "normalized_role": normalized_role,
        "site_settings": get_site_settings(),
        "site_announcements": [line.strip() for line in get_site_settings().get("announcement_lines", "").splitlines() if line.strip()],
    }


# =========================================================
# API GỬI OTP ĐĂNG KÝ
# =========================================================

@app.post("/send-otp")
def send_register_otp():
    data = get_json_data()

    email = normalize_email(
        str(data.get("email", ""))
    )

    purpose = str(
        data.get(
            "purpose",
            "register",
        )
    ).strip()

    if not email:
        return json_error(
            "Vui lòng nhập Gmail."
        )

    if not is_valid_gmail(email):
        return json_error(
            "Vui lòng nhập đúng địa chỉ @gmail.com."
        )

    if purpose != "register":
        return json_error(
            "Mục đích gửi OTP không hợp lệ."
        )

    existing_user = User.query.filter(
        func.lower(User.email) == email
    ).first()

    if existing_user:
        return json_error(
            "Gmail này đã được đăng ký."
        )

    success, message = create_and_send_otp(
        email,
        "register",
    )

    if not success:
        return json_error(
            message,
            429 if "chờ" in message else 500,
        )

    return json_success(message)


# =========================================================
# API ĐĂNG KÝ
# =========================================================

@app.post("/register")
def register():
    data = get_json_data()

    email = normalize_email(
        str(data.get("email", ""))
    )

    username = normalize_username(
        str(data.get("username", ""))
    )

    password = str(
        data.get("password", "")
    )

    otp = str(
        data.get("otp", "")
    ).strip()

    if not email:
        return json_error(
            "Thiếu Gmail."
        )

    if not username:
        return json_error(
            "Thiếu tên người dùng."
        )

    if not password:
        return json_error(
            "Thiếu mật khẩu."
        )

    if not otp:
        return json_error(
            "Thiếu mã OTP."
        )

    if not is_valid_gmail(email):
        return json_error(
            "Gmail không hợp lệ."
        )

    if not is_valid_username(username):
        return json_error(
            "Tên người dùng phải dài từ 4 đến 30 ký tự "
            "và chỉ chứa chữ, số, dấu chấm hoặc gạch dưới."
        )

    if len(password) < 8:
        return json_error(
            "Mật khẩu cần tối thiểu 8 ký tự."
        )

    if not re.fullmatch(r"\d{6}", otp):
        return json_error(
            "OTP phải gồm đúng 6 chữ số."
        )

    existing_email = User.query.filter(
        func.lower(User.email) == email
    ).first()

    if existing_email:
        return json_error(
            "Gmail này đã được đăng ký."
        )

    existing_username = User.query.filter(
        func.lower(User.username)
        == username.lower()
    ).first()

    if existing_username:
        return json_error(
            "Tên người dùng đã được sử dụng."
        )

    otp_valid, otp_message = verify_otp_code(
        email=email,
        purpose="register",
        otp=otp,
        mark_used=False,
    )

    if not otp_valid:
        return json_error(
            otp_message
        )

    user = User(
        email=email,
        username=username,
        role="buyer",
        balance=0,
        is_active=True,
    )

    user.set_password(password)

    try:
        db.session.add(user)
        db.session.flush()

        otp_record = get_latest_otp(
            email,
            "register",
        )

        if otp_record:
            otp_record.is_used = True

        db.session.commit()

    except Exception:
        db.session.rollback()

        app.logger.exception(
            "Lỗi tạo tài khoản"
        )

        return json_error(
            "Không thể tạo tài khoản. "
            "Vui lòng thử lại.",
            500,
        )

    session.clear()
    # Tài khoản vừa đăng ký được ghi nhớ trong 30 ngày trên thiết bị này.
    session.permanent = True
    session["user_id"] = user.id
    session["username"] = user.username
    session["role"] = user.role

    return json_success(
        "Xác minh thành công! "
        "Tài khoản của bạn đã được tạo.",
        redirect_url=url_for("profile"),
    )


@app.post("/logout")
def logout():
    session.clear()

    return json_success(
        "Đã đăng xuất.",
        redirect_url=url_for("home"),
    )


@app.get("/logout")
def logout_get():
    session.clear()

    return redirect(
        url_for("home")
    )


# =========================================================
# API QUÊN MẬT KHẨU — GỬI OTP
# =========================================================

@app.post("/forgot-password/send-otp")
def forgot_password_send_otp():
    data = get_json_data()

    email = normalize_email(
        str(data.get("email", ""))
    )

    if not is_valid_gmail(email):
        return json_error(
            "Vui lòng nhập đúng Gmail."
        )

    user = User.query.filter(
        func.lower(User.email) == email
    ).first()

    if not user:
        return json_error(
            "Không tìm thấy tài khoản sử dụng Gmail này.",
            404,
        )

    success, message = create_and_send_otp(
        email,
        "forgot_password",
    )

    if not success:
        return json_error(
            message,
            429 if "chờ" in message else 500,
        )

    return json_success(message)


# =========================================================
# API QUÊN MẬT KHẨU — KIỂM TRA OTP
# =========================================================

@app.post("/forgot-password/verify-otp")
def forgot_password_verify_otp():
    data = get_json_data()

    email = normalize_email(
        str(data.get("email", ""))
    )

    otp = str(
        data.get("otp", "")
    ).strip()

    if not email or not otp:
        return json_error(
            "Thiếu Gmail hoặc OTP."
        )

    valid, message = verify_otp_code(
        email=email,
        purpose="forgot_password",
        otp=otp,
        mark_used=False,
    )

    if not valid:
        return json_error(message)

    return json_success(
        "Mã OTP hợp lệ."
    )


# =========================================================
# API QUÊN MẬT KHẨU — ĐẶT LẠI
# =========================================================

@app.post("/forgot-password/reset")
def forgot_password_reset():
    data = get_json_data()

    email = normalize_email(
        str(data.get("email", ""))
    )

    otp = str(
        data.get("otp", "")
    ).strip()

    new_password = str(
        data.get("new_password", "")
    )

    if not email or not otp or not new_password:
        return json_error(
            "Thiếu dữ liệu đặt lại mật khẩu."
        )

    if len(new_password) < 8:
        return json_error(
            "Mật khẩu mới cần tối thiểu 8 ký tự."
        )

    valid, message = verify_otp_code(
        email=email,
        purpose="forgot_password",
        otp=otp,
        mark_used=False,
    )

    if not valid:
        return json_error(message)

    user = User.query.filter(
        func.lower(User.email) == email
    ).first()

    if not user:
        return json_error(
            "Không tìm thấy tài khoản.",
            404,
        )

    try:
        user.set_password(new_password)

        otp_record = get_latest_otp(
            email,
            "forgot_password",
        )

        if otp_record:
            otp_record.is_used = True

        db.session.commit()

    except Exception:
        db.session.rollback()

        app.logger.exception(
            "Lỗi đặt lại mật khẩu"
        )

        return json_error(
            "Không thể đặt lại mật khẩu.",
            500,
        )

    return json_success(
        "Đặt lại mật khẩu thành công."
    )


# =========================================================
# TRANG THÔNG TIN / CHÍNH SÁCH
# =========================================================

SITE_PAGE_DEFAULTS = {
    "support": ("Trung tâm hỗ trợ", "Mô tả các kênh hỗ trợ, thời gian phản hồi và cách gửi yêu cầu tại đây."),
    "terms": ("Điều khoản sử dụng", "Thêm điều khoản sử dụng chính thức của KY MMO tại đây."),
    "privacy": ("Chính sách quyền riêng tư", "Thêm nội dung về dữ liệu cá nhân và quyền riêng tư tại đây."),
    "refund": ("Chính sách hoàn tiền", "Thêm điều kiện, quy trình và thời hạn hoàn tiền tại đây."),
    "warranty": ("Chính sách bảo hành", "Thêm phạm vi và thời hạn bảo hành sản phẩm tại đây."),
}

def get_site_page(page_key):
    title, default_content = SITE_PAGE_DEFAULTS[page_key]
    page = SitePage.query.filter_by(page_key=page_key).first()
    if not page:
        page = SitePage(page_key=page_key, title=title, content=default_content)
        db.session.add(page)
        db.session.commit()
    return page


# =========================================================
# OAUTH GOOGLE / DISCORD
# =========================================================
def _oauth_ready(provider):
    return bool(oauth and os.getenv(f"{provider.upper()}_CLIENT_ID") and os.getenv(f"{provider.upper()}_CLIENT_SECRET"))

if oauth:
    if _oauth_ready("google"):
        oauth.register(name="google", client_id=os.getenv("GOOGLE_CLIENT_ID"), client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration", client_kwargs={"scope":"openid email profile"})
    if _oauth_ready("discord"):
        oauth.register(name="discord", client_id=os.getenv("DISCORD_CLIENT_ID"), client_secret=os.getenv("DISCORD_CLIENT_SECRET"),
            access_token_url="https://discord.com/api/oauth2/token", authorize_url="https://discord.com/oauth2/authorize",
            api_base_url="https://discord.com/api/", client_kwargs={"scope":"identify email"})

def _unique_social_username(base):
    base = normalize_username(re.sub(r"[^A-Za-z0-9._]", "", base or "user"))[:24] or "user"
    candidate = base
    while User.query.filter(func.lower(User.username)==candidate.lower()).first():
        candidate = f"{base[:20]}_{secrets.token_hex(2)}"
    return candidate

def _social_login(provider, provider_id, email, username, avatar=None):
    email = normalize_email(email)
    if not email:
        flash("Tài khoản xã hội không cung cấp email hợp lệ.", "error"); return redirect(url_for("auth"))
    id_column = User.google_id if provider == "google" else User.discord_id
    user = User.query.filter(id_column == str(provider_id)).first() or User.query.filter(func.lower(User.email)==email).first()
    if not user:
        user = User(email=email, username=_unique_social_username(username), role="buyer", balance=0, is_active=True,
                    password_hash=generate_password_hash(secrets.token_urlsafe(32)))
        db.session.add(user)
    if provider == "google": user.google_id = str(provider_id)
    else: user.discord_id = str(provider_id)
    if avatar: user.avatar_url = avatar
    db.session.commit(); session.clear(); session.permanent=True
    session.update(user_id=user.id, username=user.username, role=user.role)
    return redirect(url_for("profile"))

GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://kymmo.shop/auth/google/callback",
).strip()

@app.get("/login/google")
def login_google():
    if not _oauth_ready("google"):
        flash("Google Login chưa được cấu hình.", "error")
        return redirect(url_for("auth", mode="login"))

    # Luôn dùng callback HTTPS cố định từ biến môi trường.
    return oauth.google.authorize_redirect(GOOGLE_REDIRECT_URI)

@app.get("/auth/google/callback")
def google_callback():
    try:
        token = oauth.google.authorize_access_token()
    except MismatchingStateError:
        session.clear()
        flash("Phiên đăng nhập Google không hợp lệ hoặc đã hết hạn. Vui lòng thử lại.", "error")
        return redirect(url_for("auth", mode="login"))
    except Exception:
        app.logger.exception("Google OAuth callback thất bại")
        flash("Không thể đăng nhập bằng Google lúc này. Vui lòng thử lại.", "error")
        return redirect(url_for("auth", mode="login"))

    info = token.get("userinfo") or oauth.google.userinfo()
    return _social_login(
        "google",
        info.get("sub"),
        info.get("email"),
        info.get("name") or info.get("email", "").split("@")[0],
        info.get("picture"),
    )

@app.get("/login/discord")
def login_discord():
    if not _oauth_ready("discord"): flash("Discord Login chưa được cấu hình.", "error"); return redirect(url_for("auth"))
    return oauth.discord.authorize_redirect(url_for("discord_callback", _external=True, _scheme="https" if request.is_secure else "http"))

@app.get("/auth/discord/callback")
def discord_callback():
    oauth.discord.authorize_access_token(); info=oauth.discord.get("users/@me").json()
    avatar=f"https://cdn.discordapp.com/avatars/{info['id']}/{info['avatar']}.png" if info.get("avatar") else None
    return _social_login("discord", info.get("id"), info.get("email"), info.get("global_name") or info.get("username"), avatar)

# =========================================================
# REFUND CENTER - buyer + seller + admin cùng theo dõi
# =========================================================
ALLOWED_REFUND_EXTENSIONS={"png","jpg","jpeg","webp","gif","mp4","webm","mov"}

def _save_refund_file(file, refund_reference):
    ext=(file.filename.rsplit(".",1)[-1].lower() if "." in file.filename else "")
    if ext not in ALLOWED_REFUND_EXTENSIONS: raise ValueError("Chỉ nhận ảnh PNG/JPG/WEBP/GIF hoặc video MP4/WEBM/MOV.")
    data=file.read()
    if len(data)>20*1024*1024: raise ValueError("Mỗi tệp tối đa 20 MB.")
    object_path=f"refunds/{refund_reference}/{secrets.token_hex(8)}-{secure_filename(file.filename)}"
    _supabase_storage_request("POST", object_path, data, file.mimetype or "application/octet-stream")
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_STORAGE_BUCKET}/{quote(object_path)}", ("video" if ext in {"mp4","webm","mov"} else "image")

@app.route("/refund", methods=["GET","POST"])
@login_required
def refund_center():
    user=current_user()
    if request.method=="POST":
        order_ref=request.form.get("order_reference","").strip().upper(); reason=request.form.get("reason","").strip()
        order=MarketplaceOrder.query.filter_by(reference=order_ref, buyer_id=user.id).first()
        if not order: flash("Không tìm thấy đơn hàng thuộc tài khoản của bạn.","error"); return redirect(url_for("refund_center"))
        if len(reason)<15: flash("Lý do refund cần ít nhất 15 ký tự.","error"); return redirect(url_for("refund_center"))
        existing=RefundRequest.query.filter_by(order_id=order.id).filter(RefundRequest.status.in_(["pending","approved"])).first()
        if existing: flash("Đơn hàng này đã có yêu cầu refund đang xử lý.","error"); return redirect(url_for("refund_center"))
        rr=RefundRequest(reference=f"RF-{datetime.now(VN_TIMEZONE):%Y%m%d}-{secrets.token_hex(3).upper()}", order_id=order.id,buyer_id=user.id,seller_id=order.seller_id,reason=reason)
        db.session.add(rr); db.session.flush()
        try:
            for f in request.files.getlist("attachments")[:6]:
                if f and f.filename:
                    url,typ=_save_refund_file(f,rr.reference); db.session.add(RefundAttachment(refund_id=rr.id,file_url=url,file_type=typ,original_name=secure_filename(f.filename)))
            db.session.commit()
        except Exception as exc:
            db.session.rollback(); app.logger.exception("Upload bằng chứng refund thất bại"); flash(str(exc),"error"); return redirect(url_for("refund_center"))
        send_discord_webhook("💰 YÊU CẦU REFUND MỚI",f"**Refund:** {rr.reference}\n**Đơn:** {order.reference}\n**Buyer:** {user.username}\n**Seller ID:** {order.seller_id}\n**Lý do:** {reason}\nCần Seller và Admin/Founder cùng duyệt.",0xF39C12)
        flash("Đã gửi yêu cầu refund.","success"); return redirect(url_for("refund_center"))
    requests_q=RefundRequest.query.filter_by(buyer_id=user.id).order_by(RefundRequest.created_at.desc()).all()
    return render_template("refund-center.html",current_user=user,refunds=requests_q)

@app.get("/seller/refunds")
@login_required
def seller_refunds():
    user=current_user()
    if normalized_role(user) not in {"seller","admin","founder"}: return redirect(url_for("profile"))
    rows=RefundRequest.query.filter_by(seller_id=user.id).order_by(RefundRequest.created_at.desc()).all()
    return render_template("refund-manage.html",current_user=user,refunds=rows,mode="seller")

@app.post("/seller/refunds/<int:refund_id>/<action>")
@login_required
def seller_refund_action(refund_id,action):
    user=current_user(); rr=RefundRequest.query.get_or_404(refund_id)
    if rr.seller_id!=user.id or action not in {"approve","reject"}: flash("Không có quyền xử lý.","error"); return redirect(url_for("seller_refunds"))
    rr.seller_decision=action; rr.seller_note=request.form.get("note","").strip() or None
    _finalize_refund(rr); db.session.commit(); return redirect(url_for("seller_refunds"))

@app.get("/admin/refunds")
@admin_required
def admin_refunds():
    rows=RefundRequest.query.order_by(RefundRequest.created_at.desc()).all()
    return render_template("refund-manage.html",current_user=current_user(),refunds=rows,mode="admin")

@app.post("/admin/refunds/<int:refund_id>/<action>")
@admin_required
def admin_refund_action(refund_id,action):
    rr=RefundRequest.query.get_or_404(refund_id)
    if action not in {"approve","reject"}: return redirect(url_for("admin_refunds"))
    rr.admin_decision=action; rr.admin_note=request.form.get("note","").strip() or None; rr.processed_by=current_user().id
    _finalize_refund(rr); db.session.commit(); return redirect(url_for("admin_refunds"))

def _finalize_refund(rr):
    if "reject" in {rr.seller_decision,rr.admin_decision}: rr.status="rejected"; return
    if rr.seller_decision=="approve" and rr.admin_decision=="approve":
        order=db.session.get(MarketplaceOrder,rr.order_id); buyer=db.session.get(User,rr.buyer_id)
        if rr.status!="approved":
            buyer.balance=int(buyer.balance or 0)+int(order.amount); order.status="refunded"; rr.status="approved"
            hold=SellerHold.query.filter_by(order_reference=order.reference).first()
            if hold:
                wallet=SellerWallet.query.filter_by(seller_id=rr.seller_id).first()
                if hold.status=="holding":
                    hold.status="refunded"
                    if wallet: wallet.pending_balance=max(0,int(wallet.pending_balance or 0)-int(hold.seller_amount or 0))
                elif hold.status=="released" and wallet:
                    if int(wallet.available_balance or 0) < int(hold.seller_amount or 0):
                        raise ValueError("Số dư khả dụng của Seller không đủ để hoàn tiền.")
                    wallet.available_balance=int(wallet.available_balance or 0)-int(hold.seller_amount or 0)
                    hold.status="refunded"
            send_discord_webhook("✅ REFUND ĐÃ HOÀN TẤT",f"**Refund:** {rr.reference}\n**Đơn:** {order.reference}\n**Số tiền:** {int(order.amount):,}đ",0x2ECC71)

@app.post("/seller-welcome/dismiss")
@login_required
def dismiss_seller_welcome():
    user=current_user(); user.seller_welcome_pending=False; db.session.commit(); return jsonify({"success":True})

@app.get("/support")
def support_page():
    page = get_site_page("support")
    return render_template("info-page.html", current_user=current_user(), page=page)

@app.get("/terms")
def terms_page():
    page = get_site_page("terms")
    return render_template("info-page.html", current_user=current_user(), page=page)

@app.get("/privacy")
def privacy_page():
    page = get_site_page("privacy")
    return render_template("info-page.html", current_user=current_user(), page=page)

@app.get("/refund-policy")
def refund_policy_page():
    page = get_site_page("refund")
    return render_template("info-page.html", current_user=current_user(), page=page)

@app.get("/warranty")
def warranty_page():
    page = get_site_page("warranty")
    return render_template("info-page.html", current_user=current_user(), page=page)

@app.route("/admin/site-content", methods=["GET", "POST"])
@admin_required
def admin_site_content():
    if request.method == "POST":
        page_key = request.form.get("page_key", "").strip()
        if page_key not in SITE_PAGE_DEFAULTS:
            flash("Trang nội dung không hợp lệ.", "error")
            return redirect(url_for("admin_site_content"))
        page = get_site_page(page_key)
        page.title = request.form.get("title", "").strip() or SITE_PAGE_DEFAULTS[page_key][0]
        page.content = request.form.get("content", "").strip()
        db.session.commit()
        flash("Đã lưu nội dung trang.", "success")
        return redirect(url_for("admin_site_content", page=page_key))
    pages = {key: get_site_page(key) for key in SITE_PAGE_DEFAULTS}
    active_key = request.args.get("page", "terms")
    if active_key not in pages:
        active_key = "terms"
    return render_template("admin-site-content.html", current_user=current_user(), pages=pages, active_key=active_key)


@app.route("/admin/site-settings", methods=["GET", "POST"])
@admin_required
def admin_site_settings():
    if request.method == "POST":
        for key, default in SITE_SETTING_DEFAULTS.items():
            value = request.form.get(key, default).strip()
            row = SiteSetting.query.filter_by(setting_key=key).first()
            if row is None:
                row = SiteSetting(setting_key=key, setting_value=value)
                db.session.add(row)
            else:
                row.setting_value = value
        db.session.commit()
        flash("Đã lưu cài đặt website.", "success")
        return redirect(url_for("admin_site_settings"))
    return render_template("admin-site-settings.html", current_user=current_user(), settings=get_site_settings())

# =========================================================
# NẠP TIỀN THỦ CÔNG
# =========================================================

from deposit import init_deposit

DepositRequest = init_deposit(app, db, User, mail)


# =========================================================
# KHỞI TẠO DATABASE
# =========================================================

def run_lightweight_database_migrations() -> None:
    """Bổ sung các cột/index mới cho database cũ.

    ``db.create_all()`` chỉ tạo bảng chưa tồn tại, không tự thêm cột vào
    bảng đã có. Các câu lệnh PostgreSQL dưới đây có thể chạy lặp lại an toàn.
    """
    statements = [
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS category VARCHAR(60) NOT NULL DEFAULT 'Dịch vụ'",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS image_url TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS canonical_key VARCHAR(180)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS discord_id VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS seller_welcome_pending BOOLEAN NOT NULL DEFAULT FALSE",
        "CREATE INDEX IF NOT EXISTS ix_products_canonical_key ON products (canonical_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_id ON users (google_id) WHERE google_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_discord_id ON users (discord_id) WHERE discord_id IS NOT NULL",
    ]

    try:
        for statement in statements:
            db.session.execute(db.text(statement))

        # Bù canonical_key cho sản phẩm cũ để hệ thống nhiều offer hoạt động.
        products_without_key = Product.query.filter(
            (Product.canonical_key.is_(None)) | (Product.canonical_key == "")
        ).all()
        for product in products_without_key:
            product.canonical_key = canonical_product_key(product.name)

        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Không thể chạy migration database khi khởi động")
        raise


with app.app_context():
    db.create_all()
    run_lightweight_database_migrations()


# =========================================================
# CHẠY LOCAL
# =========================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "5000",
            )
        ),
        debug=(
            os.getenv(
                "FLASK_DEBUG",
                "True",
            ).lower()
            == "true"
        ),
    )