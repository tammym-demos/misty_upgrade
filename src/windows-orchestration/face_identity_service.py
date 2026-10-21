"""Explicit spoken photo enrollment and fresh local identity requests."""

from __future__ import annotations

import logging
import os
import re

import requests

import config_defaults

logger = logging.getLogger(__name__)
_NAME = re.compile(r"[A-Za-z][A-Za-z '\-]{0,39}")
_ENROLL = re.compile(
    r"^(?:please\s+)?take (?:a )?(?:picture|photo) of "
    r"(?:this person|me|them|him|her) and remember (?:them|me|him|her)"
    r"(?P<suffix>.*)$", re.IGNORECASE,
)
_NAME_CLAUSE = re.compile(
    r"^[\s.,;:!]+(?:their|there|my|his|her) name is\s+(?P<name>.+?)\s*[.!?]*$",
    re.IGNORECASE,
)
_FORGET = re.compile(
    r"^(?:please\s+)?(?:forget the face named|delete the face profile for)\s+"
    r"(?P<name>.+?)\s*[.!?]*$", re.IGNORECASE,
)
_IDENTITY = re.compile(
    r"^(?:who (?:am i|do you see)|do you (?:recognize|remember) me|"
    r"do you know who i am)[\s.!?]*$", re.IGNORECASE,
)
_GREETER = re.compile(
    r"^(?:please\s+)?(?:(?:turn|switch)\s+(?:the\s+)?greeter mode\s+(?P<toggle>on|off)"
    r"|(?P<action>start|stop|enable|disable)\s+(?:the\s+)?greeter mode)[\s.!?]*$",
    re.IGNORECASE,
)


def greeter_response_for_query(user_text: str) -> str | None:
    command = _GREETER.fullmatch((user_text or "").strip())
    if command is None:
        return None
    enabled = (command["toggle"] or command["action"]).lower() in ("on", "start", "enable")
    try:
        port = int(os.getenv("CONTROLLER_API_PORT", str(config_defaults.CONTROLLER_API_PORT)))
        response = requests.post(
            f"http://127.0.0.1:{port}/api/greeter",
            json={"enabled": enabled}, timeout=5,
        )
        response.raise_for_status()
        result = response.json()
        if (
            not isinstance(result, dict) or result.get("status") != "ok"
            or result.get("greeter_mode") is not enabled
        ):
            raise ValueError("Controller did not confirm Greeter Mode")
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning("Greeter Mode request failed: %s", exc)
        return "I couldn't change Greeter Mode. Please check my controller and local face detector."
    return (
        "Greeter Mode is on. I'll look for visitors, then say hello and wave when I'm free."
        if enabled else "Greeter Mode is off. I'll only use the camera when you ask."
    )


def normalize_person_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    return name if _NAME.fullmatch(name) else None


def face_response_for_query(user_text: str) -> str | None:
    """Only explicit commands can create or delete a profile; ordinary chat cannot."""
    text = (user_text or "").strip()
    enrollment = _ENROLL.fullmatch(text)
    forgetting = _FORGET.fullmatch(text)
    if enrollment:
        clause = _NAME_CLAUSE.fullmatch(enrollment["suffix"])
        name = normalize_person_name(clause["name"]) if clause else None
        if name is None:
            return "Please give a short name, saying: Their name is, followed by the name."
        action = "enroll"
    elif forgetting:
        name = normalize_person_name(forgetting["name"])
        if name is None:
            return "Please give the name of the face profile to delete."
        action = "forget"
    elif _IDENTITY.fullmatch(text):
        name = None
        action = "recognize"
    else:
        return None

    enabled = os.getenv(
        "USE_LAPTOP_FACE_RECOGNITION", str(config_defaults.USE_LAPTOP_FACE_RECOGNITION)
    ).lower() in ("1", "true", "yes")
    if not enabled and action != "forget":
        return "Local face recognition isn't enabled yet."
    try:
        port = int(os.getenv("CONTROLLER_API_PORT", str(config_defaults.CONTROLLER_API_PORT)))
        response = requests.post(
            f"http://127.0.0.1:{port}/api/faces/{action}",
            json={"name": name} if name is not None else {}, timeout=30,
        )
        result = response.json()
        if response.status_code == 409 and result.get("error") == "profile_exists":
            return f"A face profile for {name} already exists. Delete it explicitly before replacing it."
        if response.status_code == 422 and result.get("error") == "face_framing":
            return "Please keep just this person facing the camera, then try the photo request again."
        response.raise_for_status()
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise ValueError("Controller did not confirm the face operation")
        returned_name = normalize_person_name(result.get("name"))
        if action == "recognize":
            if result.get("name") is None:
                return "I don't confidently recognize the person in view."
            if returned_name is None:
                raise ValueError("Invalid recognized name")
            return f"I recognize {returned_name}."
        if returned_name is None or returned_name.casefold() != name.casefold():
            raise ValueError("Controller returned a different profile name")
        if action == "enroll":
            if result.get("reference_photo_saved") is not True:
                raise ValueError("Controller did not confirm saving an actual photo")
            return f"I saved a local reference photo for {returned_name}."
        if type(result.get("deleted")) is not bool:
            raise ValueError("Controller did not confirm whether the profile was deleted")
        return (
            f"I deleted the local face profile and any reference photo for {returned_name}."
            if result["deleted"] else f"I have no local face profile for {returned_name}."
        )
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        logger.warning("Local face %s request failed: %s", action, exc)
        return "I couldn't complete that local face request. Please check my face recognition setup."
