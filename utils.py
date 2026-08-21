import re
import random

PHONE_REGEX = re.compile(r"^(06|07)\d{8}$")

def validate_phone(phone: str) -> tuple[bool, str]:
    phone = phone.strip().replace(" ", "").replace("-", "")
    if not PHONE_REGEX.match(phone):
        return False, "Le numéro doit commencer par **06** ou **07** et contenir exactement **10 chiffres**."
    suffix = phone[2:]
    if len(set(suffix)) == 1:
        return False, "Ce numéro est invalide (chiffres répétés)."
    if suffix in ["12345678","23456789","34567890","87654321","98765432","09876543"]:
        return False, "Ce numéro est invalide (pattern séquentiel)."
    if suffix[:2] == suffix[2:4] == suffix[4:6] == suffix[6:8]:
        return False, "Ce numéro est invalide (pattern répété)."
    return True, ""

def mask_phone(phone: str) -> str:
    return phone[:2] + "******" + phone[-2:]

def generate_code() -> str:
    while True:
        code = f"{random.randint(0, 9999):04d}"
        if len(set(code)) == 1:
            continue
        if code in ["1234","2345","3456","4567","5678","6789","7890",
                    "4321","5432","6543","7654","8765","9876","0987"]:
            continue
        return code

def validate_code(code: str) -> tuple[bool, str]:
    code = code.strip()
    if not code.isdigit() or len(code) != 4:
        return False, "Veuillez écrire uniquement le code de vérification à **4 chiffres**."
    if len(set(code)) == 1:
        return False, "Code invalide (chiffres répétés)."
    if code in ["1234","2345","3456","4567","5678","6789","7890",
                "4321","5432","6543","7654","8765","9876","0987"]:
        return False, "Code invalide (pattern séquentiel)."
    return True, ""