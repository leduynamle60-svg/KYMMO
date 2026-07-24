from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_mail import Message
from sqlalchemy import select


def init_deposit(app, db, User, mail):
    """
    Khởi tạo hệ thống nạp tiền thủ công.

    Phân quyền trong module:
    - founder/owner: toàn quyền nạp tiền, duyệt, từ chối.
    - admin: xem, duyệt và từ chối yêu cầu nạp.
    - support: chỉ được xem danh sách, không được cộng tiền.
    - member/user: chỉ tạo và xem yêu cầu của chính mình.
    """

    deposit_bp = Blueprint("deposit", __name__)

    bank_name = os.getenv("DEPOSIT_BANK_NAME", "ACB").strip()
    bank_bin = os.getenv("DEPOSIT_BANK_BIN", "970416").strip()
    bank_account_number = os.getenv("DEPOSIT_BANK_ACCOUNT_NUMBER", "").strip()
    bank_account_name = os.getenv("DEPOSIT_BANK_ACCOUNT_NAME", "").strip()
    discord_webhook_url = os.getenv("DEPOSIT_DISCORD_WEBHOOK_URL", "").strip()

    try:
        min_deposit = Decimal(os.getenv("MIN_DEPOSIT", "10000"))
    except InvalidOperation:
        min_deposit = Decimal("10000")

    try:
        max_deposit = Decimal(os.getenv("MAX_DEPOSIT", "10000000"))
    except InvalidOperation:
        max_deposit = Decimal("10000000")

    vietnam_tz = ZoneInfo("Asia/Ho_Chi_Minh")

    try:
        deposit_expire_minutes = max(1, int(os.getenv("DEPOSIT_EXPIRE_MINUTES", "30")))
    except ValueError:
        deposit_expire_minutes = 30

    def utcnow():
        # Lưu UTC trong database để nhất quán khi deploy.
        return datetime.now(timezone.utc)

    def vietnam_time(value):
        """Đổi datetime từ UTC sang giờ Việt Nam (UTC+7) để hiển thị."""
        if value is None:
            return None

        # Một số driver trả datetime không kèm tzinfo dù cột có timezone=True.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        return value.astimezone(vietnam_tz)

    class DepositRequest(db.Model):
        __tablename__ = "deposit_requests"
        __table_args__ = {"extend_existing": True}

        id = db.Column(db.Integer, primary_key=True)

        user_id = db.Column(
            db.Integer,
            db.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )

        code = db.Column(
            db.String(30),
            unique=True,
            nullable=False,
            index=True,
        )

        amount = db.Column(db.BigInteger, nullable=False)

        bank_name = db.Column(db.String(100), nullable=False)
        account_number = db.Column(db.String(100), nullable=False)
        account_name = db.Column(db.String(150), nullable=False)

        transfer_content = db.Column(
            db.String(100),
            unique=True,
            nullable=False,
        )

        status = db.Column(
            db.String(20),
            nullable=False,
            default="processing",
            index=True,
        )

        failure_reason = db.Column(db.Text, nullable=True)

        created_at = db.Column(
            db.DateTime(timezone=True),
            nullable=False,
            default=utcnow,
        )

        processed_at = db.Column(
            db.DateTime(timezone=True),
            nullable=True,
        )

        processed_by = db.Column(
            db.Integer,
            nullable=True,
        )

        wallet_credited = db.Column(
            db.Boolean,
            nullable=False,
            default=False,
        )

        user = db.relationship(
            User,
            backref=db.backref("deposit_requests", lazy="dynamic"),
        )

    def current_user():
        user_id = session.get("user_id")
        if not user_id:
            return None

        user = db.session.get(User, user_id)

        if user is None:
            session.clear()

        return user

    def normalize_role(user):
        if not user:
            return "guest"

        role = str(getattr(user, "role", "member") or "member").strip().lower()

        if role == "owner":
            return "founder"

        if role == "user":
            return "member"

        return role

    def is_founder(user):
        return normalize_role(user) == "founder"

    def can_view_deposits(user):
        return normalize_role(user) in {"founder", "admin", "support"}

    def can_manage_deposits(user):
        return normalize_role(user) in {"founder", "admin"}

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()

            if user is None:
                flash("Vui lòng đăng nhập để tiếp tục.", "error")
                return redirect(url_for("auth", mode="login"))

            return view(*args, **kwargs)

        return wrapped

    def deposit_view_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()

            if user is None:
                flash("Vui lòng đăng nhập để tiếp tục.", "error")
                return redirect(url_for("auth", mode="login"))

            if not can_view_deposits(user):
                flash("Bạn không có quyền truy cập trang quản lý nạp tiền.", "error")
                return redirect(url_for("profile"))

            return view(*args, **kwargs)

        return wrapped

    def deposit_manage_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()

            if user is None:
                return "Vui lòng đăng nhập.", 401

            if not can_manage_deposits(user):
                return "Bạn không có quyền duyệt hoặc từ chối yêu cầu nạp tiền.", 403

            return view(*args, **kwargs)

        return wrapped

    def deposit_deadline(deposit):
        created_at = deposit.created_at or utcnow()
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at + timedelta(minutes=deposit_expire_minutes)

    def is_deposit_expired(deposit, now=None):
        if deposit.status != "processing":
            return False
        return (now or utcnow()) >= deposit_deadline(deposit)

    def expire_stale_deposits(user_id=None):
        query = select(DepositRequest).where(DepositRequest.status == "processing")
        if user_id is not None:
            query = query.where(DepositRequest.user_id == user_id)
        changed = False
        now = utcnow()
        for item in db.session.scalars(query).all():
            if is_deposit_expired(item, now):
                item.status = "failed"
                item.failure_reason = (
                    f"Mã nạp đã hết hiệu lực sau {deposit_expire_minutes} phút."
                )
                item.processed_at = now
                item.wallet_credited = False
                changed = True
        if changed:
            db.session.commit()
        return changed

    def make_code():
        while True:
            code = f"NAP{secrets.randbelow(900000) + 100000}"

            exists = db.session.scalar(
                select(DepositRequest.id).where(
                    DepositRequest.code == code
                )
            )

            if not exists:
                return code

    def money(value):
        return f"{int(value or 0):,}".replace(",", ".") + "₫"

    def qr_url(deposit):
        if not bank_account_number:
            return ""

        account_name_q = quote(bank_account_name)
        transfer_content_q = quote(deposit.transfer_content)

        return (
            f"https://img.vietqr.io/image/"
            f"{bank_bin}-{bank_account_number}-compact2.png"
            f"?amount={int(deposit.amount)}"
            f"&addInfo={transfer_content_q}"
            f"&accountName={account_name_q}"
        )

    def send_webhook(deposit, user, title, description, color):
        """
        Gửi log Discord.

        Lỗi webhook không làm hỏng quá trình tạo/duyệt yêu cầu nạp tiền.
        """

        if not discord_webhook_url:
            return

        payload = {
            "username": "KY MMO Deposit",
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                    "fields": [
                        {
                            "name": "Khách hàng",
                            "value": (
                                f"{getattr(user, 'username', 'Không rõ')} "
                                f"({getattr(user, 'email', 'Không có email')})"
                            ),
                            "inline": False,
                        },
                        {
                            "name": "Mã nạp",
                            "value": deposit.code,
                            "inline": True,
                        },
                        {
                            "name": "Số tiền",
                            "value": money(deposit.amount),
                            "inline": True,
                        },
                        {
                            "name": "Trạng thái",
                            "value": deposit.status,
                            "inline": True,
                        },
                        {
                            "name": "Nội dung chuyển khoản",
                            "value": deposit.transfer_content,
                            "inline": False,
                        },
                    ],
                    "timestamp": utcnow().isoformat(),
                }
            ],
        }

        try:
            req = urllib.request.Request(
                discord_webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 KY-MMO/1.0",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                response.read()

        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = "Không đọc được nội dung lỗi."

            current_app.logger.error(
                "Discord webhook trả về HTTP %s: %s",
                exc.code,
                error_body,
            )

        except (urllib.error.URLError, TimeoutError):
            current_app.logger.exception(
                "Không gửi được log nạp tiền lên Discord."
            )

        except Exception:
            current_app.logger.exception(
                "Đã xảy ra lỗi không xác định khi gửi Discord webhook."
            )

    def send_status_email(user, subject, body):
        if not getattr(user, "email", None):
            return

        try:
            mail.send(
                Message(
                    subject=subject,
                    recipients=[user.email],
                    body=body,
                )
            )

        except Exception:
            current_app.logger.exception(
                "Không gửi được email trạng thái nạp tiền."
            )

    @deposit_bp.get("/wallet/deposit")
    @login_required
    def deposit_page():
        user = current_user()
        expire_stale_deposits(user.id)

        deposits = db.session.scalars(
            select(DepositRequest)
            .where(DepositRequest.user_id == user.id)
            .order_by(DepositRequest.created_at.desc())
        ).all()

        return render_template(
            "deposit.html",
            current_user=user,
            deposits=deposits,
            bank_name=bank_name,
            bank_bin=bank_bin,
            bank_account_number=bank_account_number,
            bank_account_name=bank_account_name,
            min_deposit=min_deposit,
            max_deposit=max_deposit,
            qr_url=qr_url,
            money=money,
            vietnam_time=vietnam_time,
            deposit_deadline=deposit_deadline,
            deposit_expire_minutes=deposit_expire_minutes,
        )

    @deposit_bp.post("/wallet/deposit/create")
    @login_required
    def create_deposit():
        user = current_user()

        raw_amount = (
            request.form.get("amount") or ""
        ).replace(".", "").replace(",", "").strip()

        try:
            amount = Decimal(raw_amount)
        except (InvalidOperation, ValueError):
            flash("Số tiền không hợp lệ.", "error")
            return redirect(url_for("deposit.deposit_page"))

        if amount != amount.to_integral_value():
            flash("Số tiền phải là số nguyên.", "error")
            return redirect(url_for("deposit.deposit_page"))

        if amount < min_deposit:
            flash(
                f"Số tiền nạp tối thiểu là {money(min_deposit)}.",
                "error",
            )
            return redirect(url_for("deposit.deposit_page"))

        if amount > max_deposit:
            flash(
                f"Số tiền nạp tối đa là {money(max_deposit)}.",
                "error",
            )
            return redirect(url_for("deposit.deposit_page"))

        if not bank_account_number or not bank_account_name:
            flash(
                "Hệ thống chưa cấu hình tài khoản ngân hàng.",
                "error",
            )
            return redirect(url_for("deposit.deposit_page"))

        code = make_code()

        deposit = DepositRequest(
            user_id=user.id,
            code=code,
            amount=int(amount),
            bank_name=bank_name,
            account_number=bank_account_number,
            account_name=bank_account_name,
            transfer_content=code,
            status="processing",
            wallet_credited=False,
        )

        try:
            db.session.add(deposit)
            db.session.commit()

        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Không thể tạo yêu cầu nạp tiền."
            )
            flash(
                "Không thể tạo yêu cầu nạp tiền. Vui lòng thử lại.",
                "error",
            )
            return redirect(url_for("deposit.deposit_page"))

        send_webhook(
            deposit,
            user,
            "💰 Yêu cầu nạp tiền mới",
            (
                "Khách hàng vừa tạo yêu cầu nạp tiền. "
                "Vui lòng kiểm tra giao dịch ngân hàng trước khi duyệt."
            ),
            0xF1C40F,
        )

        flash(
            (
                "Đã tạo yêu cầu nạp tiền. "
                f"Mã có hiệu lực trong {deposit_expire_minutes} phút; "
                "vui lòng chuyển đúng số tiền và nội dung."
            ),
            "success",
        )

        return redirect(url_for("deposit.deposit_page"))

    @deposit_bp.get("/admin/deposits")
    @deposit_view_required
    def admin_deposits():
        user = current_user()
        expire_stale_deposits()

        status_filter = (
            request.args.get("status") or "all"
        ).strip().lower()

        query = select(DepositRequest).order_by(
            DepositRequest.created_at.desc()
        )

        if status_filter in {"processing", "completed", "failed"}:
            query = query.where(
                DepositRequest.status == status_filter
            )

        deposits = db.session.scalars(query).all()

        return render_template(
            "admin_deposits.html",
            current_user=user,
            deposits=deposits,
            selected_status=status_filter,
            can_manage=can_manage_deposits(user),
            is_founder=is_founder(user),
            money=money,
            vietnam_time=vietnam_time,
            deposit_deadline=deposit_deadline,
            deposit_expire_minutes=deposit_expire_minutes,
        )

    @deposit_bp.post(
        "/admin/deposits/<int:deposit_id>/approve"
    )
    @deposit_manage_required
    def approve_deposit(deposit_id):
        admin = current_user()

        try:
            deposit = db.session.execute(
                select(DepositRequest)
                .where(DepositRequest.id == deposit_id)
                .with_for_update()
            ).scalar_one_or_none()

            if not deposit:
                flash("Không tìm thấy yêu cầu nạp tiền.", "error")
                return redirect(url_for("deposit.admin_deposits"))

            if is_deposit_expired(deposit):
                deposit.status = "failed"
                deposit.failure_reason = (
                    f"Mã nạp đã hết hiệu lực sau {deposit_expire_minutes} phút."
                )
                deposit.processed_at = utcnow()
                db.session.commit()
                flash("Mã nạp này đã hết hiệu lực, không thể duyệt.", "error")
                return redirect(url_for("deposit.admin_deposits"))

            if (
                deposit.status != "processing"
                or deposit.wallet_credited
            ):
                flash(
                    "Yêu cầu này đã được xử lý trước đó.",
                    "error",
                )
                return redirect(url_for("deposit.admin_deposits"))

            customer = db.session.execute(
                select(User)
                .where(User.id == deposit.user_id)
                .with_for_update()
            ).scalar_one_or_none()

            if not customer:
                flash(
                    "Không tìm thấy tài khoản khách hàng.",
                    "error",
                )
                return redirect(url_for("deposit.admin_deposits"))

            old_balance = int(
                getattr(customer, "balance", 0) or 0
            )

            new_balance = old_balance + int(deposit.amount)

            customer.balance = new_balance

            deposit.status = "completed"
            deposit.processed_at = utcnow()
            deposit.processed_by = admin.id
            deposit.wallet_credited = True
            deposit.failure_reason = None

            db.session.commit()

        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Lỗi duyệt yêu cầu nạp tiền."
            )
            flash(
                "Không thể duyệt yêu cầu nạp tiền.",
                "error",
            )
            return redirect(url_for("deposit.admin_deposits"))

        send_webhook(
            deposit,
            customer,
            "✅ Nạp tiền hoàn thành",
            (
                f"Quản trị viên {getattr(admin, 'username', admin.id)} "
                f"đã duyệt yêu cầu và cộng {money(deposit.amount)} "
                "vào ví khách hàng."
            ),
            0x2ECC71,
        )

        send_status_email(
            customer,
            f"KY MMO - Nạp tiền thành công {deposit.code}",
            (
                f"Xin chào {customer.username},\n\n"
                f"Yêu cầu {deposit.code} đã được duyệt.\n"
                f"Số tiền: {money(deposit.amount)}\n"
                f"Số dư hiện tại: {money(customer.balance)}\n\n"
                "Cảm ơn bạn đã sử dụng KY MMO."
            ),
        )

        flash(
            (
                f"Đã duyệt {deposit.code} và cộng "
                f"{money(deposit.amount)} vào ví khách hàng."
            ),
            "success",
        )

        return redirect(url_for("deposit.admin_deposits"))

    @deposit_bp.post(
        "/admin/deposits/<int:deposit_id>/reject"
    )
    @deposit_manage_required
    def reject_deposit(deposit_id):
        admin = current_user()

        reason = (
            request.form.get("reason") or ""
        ).strip()

        if len(reason) < 3:
            flash(
                "Vui lòng nhập lý do từ chối rõ ràng.",
                "error",
            )
            return redirect(url_for("deposit.admin_deposits"))

        try:
            deposit = db.session.execute(
                select(DepositRequest)
                .where(DepositRequest.id == deposit_id)
                .with_for_update()
            ).scalar_one_or_none()

            if not deposit:
                flash("Không tìm thấy yêu cầu nạp tiền.", "error")
                return redirect(url_for("deposit.admin_deposits"))

            if (
                deposit.status != "processing"
                or deposit.wallet_credited
            ):
                flash(
                    "Yêu cầu này đã được xử lý trước đó.",
                    "error",
                )
                return redirect(url_for("deposit.admin_deposits"))

            customer = db.session.get(
                User,
                deposit.user_id,
            )

            deposit.status = "failed"
            deposit.failure_reason = reason
            deposit.processed_at = utcnow()
            deposit.processed_by = admin.id
            deposit.wallet_credited = False

            db.session.commit()

        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Lỗi từ chối yêu cầu nạp tiền."
            )
            flash(
                "Không thể từ chối yêu cầu nạp tiền.",
                "error",
            )
            return redirect(url_for("deposit.admin_deposits"))

        if customer:
            send_webhook(
                deposit,
                customer,
                "❌ Yêu cầu nạp tiền bị từ chối",
                (
                    f"Quản trị viên "
                    f"{getattr(admin, 'username', admin.id)} "
                    f"đã từ chối yêu cầu.\nLý do: {reason}"
                ),
                0xE74C3C,
            )

            send_status_email(
                customer,
                f"KY MMO - Yêu cầu nạp bị từ chối {deposit.code}",
                (
                    f"Xin chào {customer.username},\n\n"
                    f"Yêu cầu {deposit.code} không được duyệt.\n"
                    f"Số tiền: {money(deposit.amount)}\n"
                    f"Lý do: {reason}\n\n"
                    "Vui lòng kiểm tra lại giao dịch hoặc liên hệ hỗ trợ."
                ),
            )

        flash(
            f"Đã từ chối yêu cầu {deposit.code}.",
            "success",
        )

        return redirect(url_for("deposit.admin_deposits"))

    app.register_blueprint(deposit_bp)

    # Cho app.py hoặc module khác có thể dùng lại model và hàm phân quyền.
    app.extensions["deposit_model"] = DepositRequest
    app.extensions["deposit_permissions"] = {
        "normalize_role": normalize_role,
        "is_founder": is_founder,
        "can_view_deposits": can_view_deposits,
        "can_manage_deposits": can_manage_deposits,
    }

    return DepositRequest
