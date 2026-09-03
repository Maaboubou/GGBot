#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
邮件通知服务 - 适合新架构
基于legacy/Email_notify.py，但使用数据库配置管理
支持独立运行（如 Ping_notify.py），此时从环境变量读取配置。
"""

import html
import logging
import mimetypes
import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

# config_service 依赖数据库，独立运行时可能不可用，做安全导入
try:
    from .config_service import get_setting as _get_setting_from_db
except Exception:
    _get_setting_from_db = None  # type: ignore

logger = logging.getLogger(__name__)


class EmailService:
    """邮件服务类"""

    def _get_email_address(self) -> Optional[str]:
        """获取发件地址（优先数据库，回退到环境变量）。"""
        email_address: Optional[str] = None
        if _get_setting_from_db is not None:
            try:
                email_address = _get_setting_from_db("QQEMAIL_ADDR")
            except Exception:
                pass
        if not email_address:
            email_address = os.getenv("QQEMAIL_ADDR")
        return str(email_address or "").strip() or None

    def _get_auth_code(self) -> Optional[str]:
        """获取邮箱授权码（优先数据库，回退到环境变量）"""
        auth_code: Optional[str] = None

        # 优先从数据库获取
        if _get_setting_from_db is not None:
            try:
                auth_code = _get_setting_from_db("QQEMAIL_CODE")
            except Exception:
                pass

        # 回退到环境变量
        if not auth_code:
            auth_code = os.getenv("QQEMAIL_CODE")

        return auth_code

    def send_email(
        self,
        body_text: str,
        subject: str = "微信助手通知",
        attachment_paths: Optional[Iterable[str | os.PathLike[str]]] = None,
        body_html: Optional[str] = None,
        inline_image_paths: Optional[
            Iterable[tuple[str, str | os.PathLike[str]]]
        ] = None,
    ) -> bool:
        """
        发送邮件

        Args:
            body_text: 正文内容
            subject: 邮件标题，默认为 "微信助手通知"
            attachment_paths: 可选附件路径列表
            body_html: 可选 HTML 正文；提供时与纯文本正文组成兼容内容
            inline_image_paths: 可选的 ``(Content-ID, 图片路径)`` 列表

        Returns:
            bool: 发送成功返回True，失败返回False
        """
        qq_email = self._get_email_address()
        if not qq_email:
            logger.error("❌ 邮箱地址未配置，请设置 QQEMAIL_ADDR")
            return False

        auth_code = self._get_auth_code()
        if not auth_code:
            logger.error("❌ 邮件授权码未配置，请设置环境变量 QQEMAIL_CODE")
            return False

        try:
            msg = MIMEMultipart("mixed")
            msg['From'] = qq_email
            msg['To'] = qq_email  # 发给自己
            msg['Subject'] = subject

            inline_images = tuple(inline_image_paths or ())
            if body_html or inline_images:
                related = MIMEMultipart("related")
                alternatives = MIMEMultipart("alternative")
                alternatives.attach(MIMEText(body_text, 'plain', 'utf-8'))
                if body_html:
                    alternatives.attach(MIMEText(body_html, 'html', 'utf-8'))
                related.attach(alternatives)

                for content_id, inline_image_path in inline_images:
                    path = Path(inline_image_path)
                    content_type, _encoding = mimetypes.guess_type(path.name)
                    if not content_type or not content_type.startswith("image/"):
                        raise ValueError(f"内嵌内容不是受支持的图片: {path.name}")
                    _maintype, subtype = content_type.split("/", 1)
                    part = MIMEImage(path.read_bytes(), _subtype=subtype)
                    part.add_header("Content-ID", f"<{content_id}>")
                    part.add_header(
                        "Content-Disposition",
                        "inline",
                        filename=path.name,
                    )
                    part.add_header("Content-Location", path.name)
                    related.attach(part)

                msg.attach(related)
            else:
                msg.attach(MIMEText(body_text, 'plain', 'utf-8'))

            for attachment_path in attachment_paths or ():
                path = Path(attachment_path)
                content_type, _encoding = mimetypes.guess_type(path.name)
                if not content_type:
                    content_type = "application/octet-stream"
                maintype, subtype = content_type.split("/", 1)
                part = MIMEBase(maintype, subtype)
                part.set_payload(path.read_bytes())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=path.name,
                )
                msg.attach(part)

            server = smtplib.SMTP_SSL('smtp.qq.com', 465)
            server.login(qq_email, auth_code)
            server.send_message(msg)
            server.quit()

            logger.info(f"✅ 邮件发送成功: {subject}")
            return True

        except Exception as e:
            logger.error(f"❌ 邮件发送失败: {e}")
            return False

    def send_offline_notification(self, bot_name: str = "微信助手") -> bool:
        """发送掉线通知邮件"""
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        email_body = f"""
{bot_name} 掉线通知

机器人名称: {bot_name}
时间: {current_time}
状态: 微信掉线 (wx.IsOnline() 返回False或异常)

请检查微信客户端状态，可能需要重新登录。

此消息由微信掉线监控系统自动发送。
        """.strip()

        return self.send_email(email_body, f"🚨 {bot_name} 掉线通知")

    def send_login_qr_notification(
        self,
        qr_image_path: str | os.PathLike[str],
        bot_name: str = "微信助手",
        reason: str = "微信要求重新扫码登录",
    ) -> bool:
        """发送正文内嵌当前登录二维码的掉线通知。"""
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        host = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "unknown"
        email_body = f"""
{bot_name} 已掉线，自动化已进入微信扫码登录页。

机器人名称: {bot_name}
主机: {host}
时间: {current_time}
状态: {reason}

请尽快使用微信扫描正文中的登录二维码。二维码可能失效；如果扫码失败，请远程查看电脑上的微信窗口。
请勿转发本邮件或其中的二维码。

此消息由微信掉线监控系统自动发送。
        """.strip()

        escaped_bot_name = html.escape(str(bot_name))
        escaped_host = html.escape(str(host))
        escaped_time = html.escape(current_time)
        escaped_reason = html.escape(str(reason))
        email_html = f"""<!doctype html>
<html lang="zh-CN">
<body style="margin:0;padding:24px;background:#f5f5f5;color:#222;font-family:'Microsoft YaHei',Arial,sans-serif;">
  <div style="max-width:620px;margin:0 auto;padding:28px;background:#fff;border-radius:12px;">
    <h2 style="margin:0 0 18px;font-size:20px;">{escaped_bot_name} 需要扫码登录</h2>
    <p style="margin:0 0 16px;line-height:1.7;">微信已掉线，自动化已进入扫码登录页。</p>
    <p style="margin:0 0 20px;line-height:1.7;">
      机器人名称：{escaped_bot_name}<br>
      主机：{escaped_host}<br>
      时间：{escaped_time}<br>
      状态：{escaped_reason}
    </p>
    <div style="margin:20px 0;text-align:center;">
      <img src="cid:wechat-login-qr" alt="微信登录二维码" width="544"
           style="display:inline-block;width:100%;max-width:544px;height:auto;border:0;image-rendering:pixelated;">
    </div>
    <p style="margin:18px 0 0;line-height:1.7;">请尽快使用微信扫描上方二维码。二维码可能失效；如果扫码失败，请远程查看电脑上的微信窗口。</p>
    <p style="margin:12px 0 0;color:#b42318;line-height:1.7;">请勿转发本邮件或其中的二维码。</p>
  </div>
</body>
</html>"""

        return self.send_email(
            email_body,
            f"🔐 {bot_name} 需要扫码登录",
            body_html=email_html,
            inline_image_paths=[("wechat-login-qr", qr_image_path)],
        )

    def send_recovery_notification(self, bot_name: str = "微信助手") -> bool:
        """发送恢复通知邮件"""
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        email_body = f"""
{bot_name} 状态已恢复正常！

机器人名称: {bot_name}
时间: {current_time}
状态: 微信在线 (wx.IsOnline() 返回True)

{bot_name} 已重新正常运行，所有功能恢复正常。

此消息由微信掉线监控系统自动发送。
        """.strip()

        return self.send_email(email_body, f"✅ {bot_name} 恢复在线")


# 全局实例
_email_service = None

def get_email_service() -> EmailService:
    """获取邮件服务实例（单例模式）"""
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service


# 便捷函数，保持与legacy代码兼容
def send_email(body_text: str, subject: str = "微信助手通知") -> bool:
    """
    便捷函数：发送邮件
    保持与legacy/Email_notify.py的接口兼容
    """
    service = get_email_service()
    return service.send_email(body_text, subject)


def send_alert_email(subject: str, body: str) -> bool:
    """
    独立运行专用便捷函数（供 Ping_notify.py 等脚本调用）。
    直接从环境变量 QQEMAIL_ADDR / QQEMAIL_CODE 读取配置，无需数据库。

    Args:
        subject: 邮件主题
        body:    邮件正文

    Returns:
        bool: 发送成功返回 True，失败返回 False
    """
    qq_email = str(os.getenv("QQEMAIL_ADDR") or "").strip()
    auth_code = os.getenv("QQEMAIL_CODE")

    if not qq_email:
        logger.error("❌ 邮箱地址未配置，请设置环境变量 QQEMAIL_ADDR")
        print("❌ 邮箱地址未配置，请设置环境变量 QQEMAIL_ADDR")
        return False

    if not auth_code:
        logger.error("❌ 邮件授权码未配置，请设置环境变量 QQEMAIL_CODE")
        print("❌ 邮件授权码未配置，请设置环境变量 QQEMAIL_CODE")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = qq_email
        msg["To"] = qq_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        server = smtplib.SMTP_SSL("smtp.qq.com", 465)
        server.login(qq_email, auth_code)
        server.send_message(msg)
        server.quit()

        logger.info(f"✅ 警报邮件发送成功: {subject}")
        print(f"✅ 邮件发送成功: {subject}")
        return True

    except Exception as e:
        logger.error(f"❌ 警报邮件发送失败: {e}")
        print(f"❌ 邮件发送失败: {e}")
        return False


if __name__ == "__main__":
    # 测试邮件发送
    service = get_email_service()

    test_body = f"""
这是一封测试邮件。

发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
测试内容: 新架构邮件功能正常

如果您收到这封邮件，说明邮件配置正确。
    """.strip()

    if service.send_email(test_body, "📧 新架构邮件功能测试"):
        print("测试邮件发送成功")
    else:
        print("测试邮件发送失败")
