"""
Family contact module.

This file intentionally does not call phone/SMS providers directly. It stores
family contacts and returns explicit action payloads, so server.py or the
frontend can ask the user for confirmation before dialing or sending a message.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


DATA_FILE = Path(__file__).resolve().parent / "contacts.json"
DEFAULT_USER_ID = "default"

RELATION_ALIASES = {
    "儿子": ["儿子", "孩子", "男孩", "小子"],
    "女儿": ["女儿", "闺女", "姑娘", "孩子"],
    "老伴": ["老伴", "爱人", "伴侣", "丈夫", "妻子", "老公", "老婆"],
    "孙子": ["孙子", "孙女", "孙辈"],
    "家属": ["家属", "家人", "亲人", "亲属"],
}

CONTACT_INTENT_PATTERNS = [
    r"联系",
    r"打电话",
    r"拨电话",
    r"通知",
    r"发短信",
    r"叫.*过来",
]

CONTACT_TARGET_HINTS = [
    "家属",
    "家人",
    "亲人",
    "联系人",
    "紧急联系人",
    "儿子",
    "女儿",
    "闺女",
    "老伴",
    "爱人",
    "孙子",
    "孙女",
    "爸爸",
    "妈妈",
]

EMERGENCY_PATTERNS = [
    r"紧急",
    r"急救",
    r"救命",
    r"不舒服",
    r"摔倒",
    r"胸口疼",
    r"喘不上气",
]


@dataclass
class Contact:
    id: str
    user_id: str
    name: str
    relation: str = ""
    phone: str = ""
    wechat: str = ""
    note: str = ""
    is_emergency: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["displayName"] = self.display_name
        return data

    @property
    def display_name(self) -> str:
        relation = self.relation.strip()
        name = self.name.strip()
        if relation and name:
            return f"{relation} {name}"
        return name or relation or "未命名联系人"


def _now() -> float:
    return time.time()


def _normalize_user_id(user_id: Optional[str]) -> str:
    return (user_id or DEFAULT_USER_ID).strip() or DEFAULT_USER_ID


def _normalize_phone(phone: str) -> str:
    return re.sub(r"[^\d+]", "", phone or "")


def _load_store() -> Dict[str, List[Dict[str, Any]]]:
    if not DATA_FILE.exists():
        return {}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[contacts] load failed: {e}")
        return {}


def _save_store(store: Dict[str, List[Dict[str, Any]]]) -> None:
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[contacts] save failed: {e}")


def _contact_from_dict(data: Dict[str, Any]) -> Contact:
    return Contact(
        id=str(data.get("id") or uuid.uuid4().hex),
        user_id=_normalize_user_id(data.get("user_id")),
        name=str(data.get("name") or "").strip(),
        relation=str(data.get("relation") or "").strip(),
        phone=_normalize_phone(str(data.get("phone") or "")),
        wechat=str(data.get("wechat") or "").strip(),
        note=str(data.get("note") or "").strip(),
        is_emergency=bool(data.get("is_emergency")),
        created_at=float(data.get("created_at") or _now()),
        updated_at=float(data.get("updated_at") or _now()),
    )


def list_contacts(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return contacts for one user, emergency contacts first."""
    uid = _normalize_user_id(user_id)
    store = _load_store()
    contacts = [_contact_from_dict(item) for item in store.get(uid, [])]
    contacts.sort(key=lambda c: (not c.is_emergency, c.relation, c.name))
    return [c.to_dict() for c in contacts]


def add_contact(
    user_id: Optional[str],
    name: str,
    relation: str = "",
    phone: str = "",
    wechat: str = "",
    note: str = "",
    is_emergency: bool = False,
) -> Dict[str, Any]:
    """Create a family contact and persist it."""
    uid = _normalize_user_id(user_id)
    contact = Contact(
        id=uuid.uuid4().hex,
        user_id=uid,
        name=(name or "").strip(),
        relation=(relation or "").strip(),
        phone=_normalize_phone(phone),
        wechat=(wechat or "").strip(),
        note=(note or "").strip(),
        is_emergency=bool(is_emergency),
        created_at=_now(),
        updated_at=_now(),
    )
    if not contact.name and not contact.relation:
        raise ValueError("联系人姓名或关系至少需要填写一个")
    if not contact.phone and not contact.wechat:
        raise ValueError("联系人电话或微信至少需要填写一个")

    store = _load_store()
    store.setdefault(uid, []).append(asdict(contact))
    _save_store(store)
    return contact.to_dict()


def update_contact(
    user_id: Optional[str],
    contact_id: str,
    **changes: Any,
) -> Optional[Dict[str, Any]]:
    """Patch one contact. Returns None when not found."""
    uid = _normalize_user_id(user_id)
    store = _load_store()
    items = store.get(uid, [])

    for idx, item in enumerate(items):
        if str(item.get("id")) != str(contact_id):
            continue

        contact = _contact_from_dict(item)
        for key in ("name", "relation", "wechat", "note"):
            if key in changes and changes[key] is not None:
                setattr(contact, key, str(changes[key]).strip())
        if "phone" in changes and changes["phone"] is not None:
            contact.phone = _normalize_phone(str(changes["phone"]))
        if "is_emergency" in changes and changes["is_emergency"] is not None:
            contact.is_emergency = bool(changes["is_emergency"])

        contact.updated_at = _now()
        items[idx] = asdict(contact)
        store[uid] = items
        _save_store(store)
        return contact.to_dict()

    return None


def remove_contact(user_id: Optional[str], contact_id: str) -> bool:
    uid = _normalize_user_id(user_id)
    store = _load_store()
    items = store.get(uid, [])
    new_items = [item for item in items if str(item.get("id")) != str(contact_id)]
    if len(new_items) == len(items):
        return False
    store[uid] = new_items
    _save_store(store)
    return True


def get_contact(user_id: Optional[str], contact_id: str) -> Optional[Dict[str, Any]]:
    for contact in list_contacts(user_id):
        if str(contact.get("id")) == str(contact_id):
            return contact
    return None


def is_contact_intent(text: str) -> bool:
    compact = (text or "").replace(" ", "")
    if not compact:
        return False
    if is_emergency_intent(compact):
        return True
    if any(word in compact for word in ("打电话", "拨电话", "发短信", "微信")):
        return True
    has_action = any(re.search(pattern, compact) for pattern in CONTACT_INTENT_PATTERNS)
    has_target = any(hint in compact for hint in CONTACT_TARGET_HINTS)
    return has_action and has_target


def is_emergency_intent(text: str) -> bool:
    compact = (text or "").replace(" ", "")
    return any(re.search(pattern, compact) for pattern in EMERGENCY_PATTERNS)


def find_contact(user_id: Optional[str], text: str = "") -> Optional[Dict[str, Any]]:
    """
    Find the most likely contact from a user utterance.

    Matching order:
    1. Emergency contact when the utterance looks urgent.
    2. Exact name/relation mentions.
    3. Relation aliases, such as "闺女" -> "女儿".
    4. First emergency contact, then first contact.
    """
    contacts = list_contacts(user_id)
    if not contacts:
        return None

    compact = (text or "").replace(" ", "")
    emergency_contacts = [c for c in contacts if c.get("is_emergency")]

    if is_emergency_intent(compact) and emergency_contacts:
        return emergency_contacts[0]

    for contact in contacts:
        name = str(contact.get("name") or "")
        relation = str(contact.get("relation") or "")
        if name and name in compact:
            return contact
        if relation and relation in compact:
            return contact

    for relation, aliases in RELATION_ALIASES.items():
        if not any(alias in compact for alias in aliases):
            continue
        for contact in contacts:
            if relation in str(contact.get("relation") or ""):
                return contact

    if emergency_contacts:
        return emergency_contacts[0]
    return contacts[0]


def build_contact_action(
    user_id: Optional[str],
    text: str = "",
    action: str = "call",
    message: str = "",
    contact_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build an action payload for server.py/frontend.

    The caller should send this payload to the browser and let the user confirm.
    Suggested frontend handling:
      - action == "call": open tel:<phone>
      - action == "sms": open sms:<phone>?body=<message>
      - action == "wechat": show the WeChat id and copy button
    """
    contact = get_contact(user_id, contact_id) if contact_id else find_contact(user_id, text)
    if not contact:
        return {
            "ok": False,
            "error": "还没有设置家属联系人",
            "action": action,
        }

    normalized_action = (action or "call").strip().lower()
    if "短信" in text or "发消息" in text:
        normalized_action = "sms"
    elif "微信" in text:
        normalized_action = "wechat"
    elif "电话" in text or "联系" in text or "通知" in text:
        normalized_action = "call"

    if normalized_action in {"call", "sms"} and not contact.get("phone"):
        return {
            "ok": False,
            "error": f"{contact.get('displayName')} 没有设置电话号码",
            "contact": contact,
            "action": normalized_action,
        }

    if normalized_action == "wechat" and not contact.get("wechat"):
        return {
            "ok": False,
            "error": f"{contact.get('displayName')} 没有设置微信号",
            "contact": contact,
            "action": normalized_action,
        }

    return {
        "ok": True,
        "action": normalized_action,
        "contact": contact,
        "phone": contact.get("phone", ""),
        "wechat": contact.get("wechat", ""),
        "message": message or build_default_message(text),
        "requiresConfirm": True,
    }


def build_default_message(text: str = "") -> str:
    reason = (text or "").strip()
    if reason:
        return f"您好，我这边可能需要家属协助。刚才的情况是：{reason}"
    return "您好，我这边可能需要家属协助，请方便时联系一下。"


def build_ack_text(action_payload: Dict[str, Any]) -> str:
    """Short spoken text for the digital human after a contact action is built."""
    if not action_payload.get("ok"):
        return action_payload.get("error") or "我还没找到可以联系的家属。"

    contact = action_payload.get("contact") or {}
    display_name = contact.get("displayName") or "家属"
    action = action_payload.get("action")
    if action == "sms":
        return f"好的，我已经帮您准备好发给{display_name}的短信了，请您确认后发送。"
    if action == "wechat":
        return f"好的，我找到{display_name}的微信了，请您确认后联系。"
    return f"好的，我找到{display_name}的电话了，请您确认后拨打。"


__all__ = [
    "Contact",
    "add_contact",
    "update_contact",
    "remove_contact",
    "get_contact",
    "list_contacts",
    "find_contact",
    "is_contact_intent",
    "is_emergency_intent",
    "build_contact_action",
    "build_default_message",
    "build_ack_text",
]
