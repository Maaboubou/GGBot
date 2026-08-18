from sqlalchemy.orm import Session
from ..models import user_permission as models

def get_user_by_chat_name(db: Session, chat_name: str):
    return db.query(models.WeChatUser).filter(models.WeChatUser.chat_name == chat_name).first()
