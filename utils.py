import re
import json
import os

PHONE_REGEX = re.compile(r"^(06|07)\d{8}$")
BLACKLIST_FILE = "blacklist.json"
SETUP_FILE = "setup_data.json"

def validate_phone(phone: str) -> tuple:
    phone = phone.strip().replace(" ", "").replace("-", "")
    if not PHONE_REGEX.match(phone):
        return False, "Le numero doit commencer par 06 ou 07 et contenir exactement 10 chiffres."
    suffix = phone[2:]
    if len(set(suffix)) == 1:
        return False, "Ce numero est invalide (chiffres repetes)."
    if suffix in ["12345678","23456789","34567890","87654321","98765432","09876543"]:
        return False, "Ce numero est invalide (pattern sequentiel)."
    if suffix[:2] == suffix[2:4] == suffix[4:6] == suffix[6:8]:
        return False, "Ce numero est invalide (pattern repete)."
    return True, ""

def mask_phone(phone: str) -> str:
    return phone[:2] + "******" + phone[-2:]

def validate_code(code: str) -> tuple:
    code = code.strip()
    if not code.isdigit() or len(code) != 4:
        return False, "Veuillez ecrire uniquement le code de verification a 4 chiffres."
    if len(set(code)) == 1:
        return False, "Code invalide (chiffres repetes)."
    if code in ["1234","2345","3456","4567","5678","6789","7890","4321","5432","6543","7654","8765","9876","0987"]:
        return False, "Code invalide (pattern sequentiel)."
    return True, ""

def load_blacklist() -> dict:
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"users": [], "phones": []}

def save_blacklist(bl: dict):
    with open(BLACKLIST_FILE, "w") as f:
        json.dump(bl, f, indent=2)

def is_user_blacklisted(user_id: int, bl: dict) -> bool:
    return user_id in bl["users"]

def is_phone_blacklisted(phone: str, bl: dict) -> bool:
    return phone in bl["phones"]

def add_to_blacklist(user_id: int, phone: str, bl: dict):
    if user_id not in bl["users"]:
        bl["users"].append(user_id)
    if phone not in bl["phones"]:
        bl["phones"].append(phone)
    save_blacklist(bl)

def remove_user_blacklist(user_id: int, bl: dict):
    if user_id in bl["users"]:
        bl["users"].remove(user_id)
        save_blacklist(bl)

def load_setup_data() -> list:
    if os.path.exists(SETUP_FILE):
        try:
            with open(SETUP_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return []

def save_setup_data(data: list):
    with open(SETUP_FILE, "w") as f:
        json.dump(data, f, indent=2)
