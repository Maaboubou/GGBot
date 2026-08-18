#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
邮件通知服务 - 适合新架构
基于legacy/Email_notify.py，但使用数据库配置管理
支持独立运行（如 Ping_notify.py），此时从环境变量读取配置。
"""

import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Optional

# config_service 依赖数据库，独立运行时可能不可用，做安全导入
try:
    from .config_service import get_setting as _get_setting_from_db
except Exception:
    _get_setting_from_db = None  # type: ignore

logger = logging.getLogger(__name__)


class EmailService:
    """邮件服务类"""
    
    def __init__(self):
        # 邮箱地址与授权码都由用户在本机配置，公开版不提供默认账号。
        qq_email: Optional[str] = None
        if _get_setting_from_db is not None:
            try:
                qq_email = _get_setting_from_db("QQEMAIL_ADDR")
            except Exception:
                pass
        self.qq_email = str(qq_email or os.getenv("QQEMAIL_ADDR") or "").strip()
        
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
    
    def send_email(self, body_text: str, subject: str = "微信助手通知") -> bool:
        """
        发送邮件
        
        Args:
            body_text: 正文内容
            subject: 邮件标题，默认为 "微信助手通知"
            
        Returns:
            bool: 发送成功返回True，失败返回False
        """
        auth_code = self._get_auth_code()
        if not self.qq_email or not auth_code:
            logger.error("❌ 邮箱或授权码未配置，请设置 QQEMAIL_ADDR 和 QQEMAIL_CODE")
            return False
            
        try:
            msg = MIMEMultipart()
            msg['From'] = self.qq_email
            msg['To'] = self.qq_email  # 发给自己
            msg['Subject'] = subject

            msg.attach(MIMEText(body_text, 'plain', 'utf-8'))

            server = smtplib.SMTP_SSL('smtp.qq.com', 465)
            server.login(self.qq_email, auth_code)
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
    直接从环境变量 QQEMAIL_CODE 读取授权码，无需数据库。

    Args:
        subject: 邮件主题
        body:    邮件正文

    Returns:
        bool: 发送成功返回 True，失败返回 False
    """
    qq_email = os.getenv("QQEMAIL_ADDR", "").strip()
    auth_code = os.getenv("QQEMAIL_CODE")

    if not qq_email or not auth_code:
        logger.error("❌ 邮箱或授权码未配置，请设置 QQEMAIL_ADDR 和 QQEMAIL_CODE")
        print("❌ 邮箱或授权码未配置，请设置 QQEMAIL_ADDR 和 QQEMAIL_CODE")
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
