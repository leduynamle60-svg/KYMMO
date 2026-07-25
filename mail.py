import os
from typing import Any

import requests


class _MailCompat:
    """Lớp tương thích Flask-Mail, nhưng gửi thực tế qua Resend API."""

    def init_app(self, app: Any) -> None:
        return None

    def send(self, message: Any) -> None:
        """Nhận đối tượng Flask-Mail Message để code cũ vẫn hoạt động."""
        recipients = list(getattr(message, "recipients", None) or [])
        if not recipients:
            raise ValueError("Email không có người nhận.")

        subject = str(getattr(message, "subject", "KY MMO") or "KY MMO")
        text_body = str(getattr(message, "body", "") or "")
        html_body = str(getattr(message, "html", "") or "")
        if not html_body:
            escaped = (
                text_body.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            html_body = f"<div style='font-family:Arial,sans-serif;line-height:1.7'>{escaped}</div>"

        for recipient in recipients:
            _send_resend_email(
                to_email=str(recipient),
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )


mail = _MailCompat()


def _send_resend_email(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> str:
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("Thiếu biến môi trường RESEND_API_KEY")

    from_email = os.getenv(
        "RESEND_FROM_EMAIL",
        "KY MMO <noreply@kymmo.shop>",
    )

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_email,
            "to": [to_email],
            "subject": subject,
            "text": text_body,
            "html": html_body,
            "reply_to": os.getenv("RESEND_REPLY_TO", "support@kymmo.shop"),
            "headers": {
                "X-Entity-Ref-ID": f"kymmo-otp-{os.urandom(8).hex()}"
            },
        },
        timeout=20,
    )

    if not response.ok:
        raise RuntimeError(
            f"Resend gửi email thất bại "
            f"(HTTP {response.status_code}): {response.text[:1000]}"
        )

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    return str(payload.get("id") or "")


def send_otp_email(
    email: str,
    otp: str,
    purpose: str,
    expire_minutes: int = 5,
) -> str:
    if purpose == "forgot_password":
        subject = "Mã đặt lại mật khẩu KY MMO"

        title = "Đặt lại mật khẩu"

        description = (
            "Bạn vừa yêu cầu đặt lại mật khẩu "
            "cho tài khoản KY MMO."
        )

    else:
        subject = "Mã xác minh tài khoản KY MMO"

        title = "Xác minh tài khoản"

        description = (
            "Bạn vừa yêu cầu đăng ký tài khoản "
            "trên KY MMO."
        )

    text_body = f"""
KY MMO

{title}

{description}

Mã OTP của bạn là:

{otp}

Mã này sẽ hết hạn sau {expire_minutes} phút.

Không chia sẻ mã OTP này cho bất kỳ ai.

Nếu bạn không thực hiện yêu cầu này,
hãy bỏ qua email.

KY MMO
"""

    html_body = f"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
</head>
<body style="
    margin:0;
    padding:30px;
    background:#060b18;
    font-family:Arial,sans-serif;
    color:#f3f6fc;
">
    <div style="
        max-width:520px;
        margin:0 auto;
        background:#0c1428;
        border:1px solid #1c2b4d;
        border-radius:18px;
        overflow:hidden;
    ">
        <div style="
            padding:26px 30px;
            background:linear-gradient(
                135deg,
                #1657ff,
                #0d3fd6
            );
        ">
            <h1 style="
                margin:0;
                font-size:23px;
            ">
                KY MMO
            </h1>

            <p style="
                margin:7px 0 0;
                opacity:.85;
                font-size:14px;
            ">
                {title}
            </p>
        </div>

        <div style="padding:30px;">
            <p style="
                color:#9aa8c7;
                line-height:1.6;
                font-size:14px;
            ">
                {description}
            </p>

            <p style="
                color:#9aa8c7;
                font-size:14px;
            ">
                Mã OTP của bạn là:
            </p>

            <div style="
                background:#060b18;
                border:1px solid #1c2b4d;
                border-radius:12px;
                padding:20px;
                text-align:center;
                margin:18px 0;
            ">
                <span style="
                    font-size:34px;
                    letter-spacing:10px;
                    font-weight:700;
                    color:#4c8dff;
                ">
                    {otp}
                </span>
            </div>

            <p style="
                color:#9aa8c7;
                font-size:13px;
                line-height:1.6;
            ">
                Mã sẽ hết hạn sau
                <strong style="color:#f3f6fc;">
                    {expire_minutes} phút
                </strong>.
            </p>

            <p style="
                color:#ff5c72;
                font-size:13px;
                line-height:1.6;
            ">
                Không chia sẻ mã OTP này
                cho bất kỳ ai.
            </p>

            <p style="
                color:#5f6d8f;
                font-size:12px;
                line-height:1.6;
                margin-top:24px;
            ">
                Nếu bạn không thực hiện yêu cầu này,
                hãy bỏ qua email.
            </p>
        </div>
    </div>
</body>
</html>
"""

    return _send_resend_email(
        to_email=email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


def send_seller_status_email(
    email: str,
    username: str,
    shop_name: str,
    approved: bool,
    note: str | None = None,
) -> None:
    """Gửi kết quả xét duyệt đăng ký Seller."""
    if approved:
        subject = "🎉 Chúc mừng! Bạn đã trở thành Seller của KY MMO"
        heading = "Yêu cầu Seller đã được duyệt"
        status_color = "#22d3a4"
        body_text = f"""Xin chào {username},

Chúc mừng! Yêu cầu mở gian hàng “{shop_name}” của bạn đã được Founder/Admin duyệt.

Hướng dẫn bắt đầu:
1. Đăng nhập KY MMO.
2. Mở menu tài khoản và chọn Seller Dashboard.
3. Nhập tên gian hàng, mô tả rõ ràng và thông tin hỗ trợ.
4. Tạo sản phẩm, nhập giá, mô tả, cách giao hàng và chính sách bảo hành.
5. Kiểm tra kỹ thông tin trước khi mở bán.

Tiền từ đơn hoàn tất sẽ được giữ 7 ngày trước khi chuyển sang số dư có thể rút để bảo vệ người mua.

KY MMO"""
        detail_html = f"""
        <p>Chúc mừng! Yêu cầu mở gian hàng <strong>{shop_name}</strong> của bạn đã được duyệt.</p>
        <h3 style="color:#f5f7ff;margin-top:24px">Hướng dẫn bắt đầu bán hàng</h3>
        <ol style="color:#a8b6d3;line-height:1.8;padding-left:20px">
          <li>Đăng nhập KY MMO và mở <strong>Seller Dashboard</strong>.</li>
          <li>Nhập tên gian hàng, mô tả rõ ràng và thông tin hỗ trợ.</li>
          <li>Tạo sản phẩm, điền giá, mô tả, cách giao hàng và bảo hành.</li>
          <li>Kiểm tra kỹ thông tin trước khi mở bán.</li>
        </ol>
        <div style="margin-top:20px;padding:14px;border-radius:10px;background:#08111f;border:1px solid #1d3150;color:#8fa4c8">
          Tiền từ đơn hoàn tất sẽ được giữ <strong style="color:#f5f7ff">7 ngày</strong> trước khi có thể rút.
        </div>
        """
    else:
        subject = "Kết quả xét duyệt Seller KY MMO"
        heading = "Yêu cầu Seller chưa được duyệt"
        status_color = "#ef476f"
        reason = note or "Founder/Admin chưa cung cấp lý do cụ thể."
        body_text = f"""Xin chào {username},

Yêu cầu mở gian hàng “{shop_name}” của bạn chưa được duyệt.

Lý do: {reason}

Bạn có thể chỉnh sửa thông tin và gửi lại yêu cầu sau.

KY MMO"""
        detail_html = f"""
        <p>Yêu cầu mở gian hàng <strong>{shop_name}</strong> của bạn chưa được duyệt.</p>
        <div style="margin-top:20px;padding:14px;border-radius:10px;background:#20101a;border:1px solid #5a2639;color:#ffc1cf">
          <strong>Lý do:</strong> {reason}
        </div>
        <p style="margin-top:20px">Bạn có thể bổ sung thông tin và gửi lại yêu cầu sau.</p>
        """

    html_body = f"""
    <!doctype html>
    <html lang="vi"><body style="margin:0;padding:30px;background:#060b18;font-family:Arial,sans-serif;color:#f3f6fc">
      <div style="max-width:620px;margin:auto;background:#0c1428;border:1px solid #1c2b4d;border-radius:18px;overflow:hidden">
        <div style="padding:26px 30px;background:#0b1830;border-bottom:3px solid {status_color}">
          <h1 style="margin:0;font-size:23px">KY MMO</h1>
          <p style="margin:8px 0 0;color:{status_color};font-weight:700">{heading}</p>
        </div>
        <div style="padding:30px;color:#a8b6d3;line-height:1.7">
          <p>Xin chào <strong style="color:#f5f7ff">{username}</strong>,</p>
          {detail_html}
          <p style="margin-top:28px;color:#657695;font-size:12px">Email tự động từ hệ thống KY MMO.</p>
        </div>
      </div>
    </body></html>
    """
    _send_resend_email(
        to_email=email,
        subject=subject,
        text_body=body_text,
        html_body=html_body,
    )



def send_order_confirmation_email(
    email: str,
    username: str,
    orders,
    products: dict,
    total: int,
    orders_url: str,
) -> None:
    """Gửi biên nhận và thông tin giao hàng sau khi thanh toán thành công."""
    from html import escape

    def money(value) -> str:
        return f"{int(value or 0):,}".replace(",", ".") + "₫"

    plain_lines = [
        "KY MMO - Xác nhận đơn hàng",
        "",
        f"Xin chào {username},",
        "",
        "Thanh toán của bạn đã thành công. Thông tin đơn hàng:",
        "",
    ]
    order_cards = []

    for order in orders:
        product = products.get(order.product_id)
        product_name = getattr(product, "name", f"Sản phẩm #{order.product_id}")
        reference = str(order.reference)
        delivery_type = str(order.delivery_type or "manual").lower()

        plain_lines.extend([
            f"Mã đơn: {reference}",
            f"Sản phẩm: {product_name}",
            f"Giá: {money(order.amount)}",
        ])

        if delivery_type == "automatic" and order.delivered_content:
            delivery_heading = "Thông tin hàng của bạn"
            delivery_note = (
                "Hàng đã được giao tự động. Hãy lưu thông tin này ở nơi an toàn."
            )
            delivered_text = str(order.delivered_content)
            plain_lines.extend([
                "Cách giao: Giao tự động",
                "Thông tin hàng:",
                delivered_text,
            ])
            secure_box = f"""
                <div style="margin-top:14px;padding:16px;background:#f6f8fb;border:1px solid #dfe5ee;border-radius:8px">
                  <div style="font-size:12px;font-weight:700;color:#667085;text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px">{delivery_heading}</div>
                  <pre style="margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.6 Consolas,Monaco,monospace;color:#172033">{escape(delivered_text)}</pre>
                </div>
            """
        else:
            delivery_heading = "Giao hàng thủ công"
            delivery_note = (
                "Một ticket riêng đã được tạo để bạn trao đổi trực tiếp với Seller. "
                "Mở mục Đơn hàng trên KY MMO để xem và nhắn tin."
            )
            plain_lines.extend([
                "Cách giao: Giao thủ công qua ticket",
                delivery_note,
            ])
            secure_box = ""

        plain_lines.extend(["", "---", ""])
        order_cards.append(f"""
          <div style="border:1px solid #e3e7ee;border-radius:10px;padding:18px;margin-top:14px;background:#ffffff">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
              <div>
                <div style="font-size:12px;color:#667085;margin-bottom:5px">Mã đơn {escape(reference)}</div>
                <div style="font-size:16px;font-weight:700;color:#101828">{escape(str(product_name))}</div>
              </div>
              <div style="font-size:15px;font-weight:700;color:#101828;white-space:nowrap">{money(order.amount)}</div>
            </div>
            <div style="margin-top:13px;padding-top:13px;border-top:1px solid #edf0f4">
              <div style="font-size:13px;font-weight:700;color:#344054">{delivery_heading}</div>
              <div style="margin-top:5px;font-size:13px;line-height:1.6;color:#667085">{delivery_note}</div>
              {secure_box}
            </div>
          </div>
        """)

    plain_lines.extend([
        f"Tổng thanh toán: {money(total)}",
        "",
        f"Xem đơn hàng: {orders_url}",
        "",
        "Không chia sẻ thông tin hàng, mã hoặc tài khoản với người khác.",
        "KY MMO",
    ])

    subject = f"KY MMO - Thanh toán thành công ({len(orders)} đơn hàng)"
    text_body = "\n".join(plain_lines)
    html_body = f"""
    <!doctype html>
    <html lang="vi">
      <body style="margin:0;padding:0;background:#f3f5f8;font-family:Arial,sans-serif;color:#101828">
        <div style="padding:32px 16px">
          <div style="max-width:650px;margin:0 auto;background:#ffffff;border:1px solid #e4e7ec;border-radius:12px;overflow:hidden">
            <div style="padding:24px 28px;background:#111827;color:#ffffff">
              <div style="font-size:20px;font-weight:800">KY MMO</div>
              <div style="margin-top:6px;font-size:13px;color:#cbd5e1">Thanh toán thành công</div>
            </div>
            <div style="padding:28px">
              <p style="margin:0 0 12px;font-size:15px">Xin chào <strong>{escape(str(username))}</strong>,</p>
              <p style="margin:0;color:#667085;font-size:14px;line-height:1.7">
                Thanh toán của bạn đã được xác nhận. Thông tin đơn hàng và hàng được giao nằm bên dưới.
              </p>

              {''.join(order_cards)}

              <div style="display:flex;justify-content:space-between;gap:16px;margin-top:20px;padding:17px 0;border-top:1px solid #e4e7ec;border-bottom:1px solid #e4e7ec">
                <span style="font-size:14px;color:#667085">Tổng thanh toán</span>
                <strong style="font-size:18px;color:#101828">{money(total)}</strong>
              </div>

              <div style="margin-top:22px">
                <a href="{escape(orders_url, quote=True)}" style="display:inline-block;padding:12px 18px;background:#2563eb;color:#ffffff;text-decoration:none;border-radius:7px;font-size:14px;font-weight:700">Xem đơn hàng</a>
              </div>

              <div style="margin-top:22px;padding:13px 15px;background:#fff8e6;border:1px solid #f4d58d;border-radius:8px;color:#7a5610;font-size:12px;line-height:1.6">
                Không chia sẻ thông tin hàng, mã kích hoạt hoặc tài khoản với người khác. KY MMO sẽ không yêu cầu bạn gửi lại mật khẩu qua tin nhắn.
              </div>

              <p style="margin:24px 0 0;color:#98a2b3;font-size:11px;line-height:1.6">
                Đây là email tự động được gửi sau khi ví KY MMO thanh toán thành công.
              </p>
            </div>
          </div>
        </div>
      </body>
    </html>
    """
    _send_resend_email(
        to_email=email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )