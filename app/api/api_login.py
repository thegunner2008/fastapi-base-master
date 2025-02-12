from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends
from fastapi_sqlalchemy import db
from pydantic import EmailStr, BaseModel

from app.core.security import create_access_token
from app.helpers.login_manager import PermissionRequired
from app.schemas.sche_base import DataResponse
from app.schemas.sche_token import Token
from app.services.srv_user import UserService

router = APIRouter()


class LoginRequest(BaseModel):
    userName: str = 'admin'
    password: str = ''


@router.get("/get_all_users")
def get_all_users():
    return UserService().get_all_users()


@router.post('', response_model=DataResponse[Token])
def login_access_token(form_data: LoginRequest):
    user = UserService().authenticate(email=form_data.userName, password=form_data.password)
    if not user:
        raise HTTPException(status_code=400, detail='Incorrect email or password')
    elif not user.is_active:
        raise HTTPException(status_code=401, detail='Inactive user')

    user.last_login = datetime.now()
    db.session.commit()

    return DataResponse().success_response({
        'accessToken': create_access_token(user_id=user.id)
    })
