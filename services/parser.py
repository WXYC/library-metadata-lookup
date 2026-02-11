"""Minimal parser models for the lookup service.

The lookup service receives pre-parsed requests from request-parser,
so it only needs the ParsedRequest model shape, not the Groq parsing logic.
"""

from enum import StrEnum

from pydantic import BaseModel


class MessageType(StrEnum):
    REQUEST = "request"
    DJ_MESSAGE = "dj_message"
    FEEDBACK = "feedback"
    OTHER = "other"


class ParsedRequest(BaseModel):
    song: str | None = None
    album: str | None = None
    artist: str | None = None
    is_request: bool = True
    message_type: MessageType = MessageType.REQUEST
    raw_message: str = ""
