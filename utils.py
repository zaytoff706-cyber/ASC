import json
import os
import re
from typing import Any

PHONE_REGEX = re.compile(r"^(06|07)\d{8}$")

BLACKLIST_FILE = "blacklist.json"
SETUP_FILE = "setup_data.json"

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".avi",
    ".mkv",
    ".wmv",
}


def validate_phone(phone: str) -> tuple[bool, str]:
    phone = phone.strip().replace(" ", "").replace("-", "")

    if not PHONE_REGEX.fullmatch(phone):
        return (
            False,
            "Le numéro doit commencer par 06 ou 07 et contenir exactement 10 chiffres.",
        )

    suffix = phone[2:]

    if len(set(suffix)) == 1:
        return False, "Ce numéro est invalide : chiffres répétés."

    invalid_patterns = {
        "12345678",
        "23456789",
        "34567890",
        "87654321",
        "98765432",
        "09876543",
    }

    if suffix in invalid_patterns:
        return False, "Ce numéro est invalide : suite de chiffres."

    if suffix[:2] == suffix[2:4] == suffix[4:6] == suffix[6:8]:
        return False, "Ce numéro est invalide : motif répété."

    return True, ""


def mask_phone(phone: str) -> str:
    if len(phone) < 4:
        return "********"

    return f"{phone[:2]}******{phone[-2:]}"


def validate_code(code: str) -> tuple[bool, str]:
    code = code.strip()

    if not code.isdigit() or len(code) != 4:
        return False, "Le code doit contenir exactement 4 chiffres."

    if len(set(code)) == 1:
        return False, "Code invalide : chiffres répétés."

    invalid_codes = {
        "1234",
        "2345",
        "3456",
        "4567",
        "5678",
        "6789",
        "7890",
        "4321",
        "5432",
        "6543",
        "7654",
        "8765",
        "9876",
        "0987",
    }

    if code in invalid_codes:
        return False, "Code invalide : suite de chiffres."

    return True, ""


def load_blacklist() -> dict[str, list[Any]]:
    if not os.path.exists(BLACKLIST_FILE):
        return {"users": [], "phones": []}

    try:
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {"users": [], "phones": []}

        data.setdefault("users", [])
        data.setdefault("phones", [])

        return data

    except (OSError, json.JSONDecodeError):
        return {"users": [], "phones": []}


def save_blacklist(blacklist: dict[str, list[Any]]) -> None:
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as file:
        json.dump(blacklist, file, indent=2, ensure_ascii=False)


def is_user_blacklisted(user_id: int, blacklist: dict) -> bool:
    return user_id in blacklist.get("users", [])


def is_phone_blacklisted(phone: str, blacklist: dict) -> bool:
    return phone in blacklist.get("phones", [])


def add_to_blacklist(
    user_id: int,
    phone: str,
    blacklist: dict,
) -> None:
    blacklist.setdefault("users", [])
    blacklist.setdefault("phones", [])

    if user_id not in blacklist["users"]:
        blacklist["users"].append(user_id)

    if phone and phone not in blacklist["phones"]:
        blacklist["phones"].append(phone)

    save_blacklist(blacklist)


def remove_user_blacklist(user_id: int, blacklist: dict) -> None:
    if user_id in blacklist.get("users", []):
        blacklist["users"].remove(user_id)
        save_blacklist(blacklist)


def load_setup_data() -> list[dict]:
    if not os.path.exists(SETUP_FILE):
        return []

    try:
        with open(SETUP_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, list) else []

    except (OSError, json.JSONDecodeError):
        return []


def save_setup_data(data: list[dict]) -> None:
    with open(SETUP_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def is_video_attachment(attachment) -> bool:
    if attachment is None:
        return False

    content_type = getattr(attachment, "content_type", None)
    if content_type and content_type.lower().startswith("video/"):
        return True

    filename = getattr(attachment, "filename", "") or ""
    extension = os.path.splitext(filename)[1].lower()

    return extension in VIDEO_EXTENSIONS
