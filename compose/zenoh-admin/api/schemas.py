from datetime import datetime

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class ShellElevateRequest(BaseModel):
    password: str


class ShellTicketResponse(BaseModel):
    ticket: str
    expires_at: datetime


class WsTicketResponse(BaseModel):
    ticket: str
    expires_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
