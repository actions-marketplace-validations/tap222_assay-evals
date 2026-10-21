"""SCIM 2.0 Provisioning and SAML 2.0 enterprise SSO support.

Implements standard SCIM 2.0 endpoints (RFC 7643 / RFC 7644) for user/group
lifecycle management from identity providers (Okta, Entra ID, Ping), and
SAML 2.0 Service Provider metadata and assertion handling.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store

log = logging.getLogger(__name__)

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_SP_CONFIG_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"


def scim_user_representation(row: dict) -> dict:
    """Format a database user row as a SCIM 2.0 User resource."""
    email = row.get("email") or ""
    name = row.get("name") or email
    active = not bool(row.get("disabled", False))
    role = row.get("role") or row.get("claims_role") or "read"

    created = row.get("created_at")
    modified = row.get("last_login_at") or created

    return {
        "schemas": [SCIM_USER_SCHEMA],
        "id": row["id"],
        "userName": email,
        "name": {
            "formatted": name,
            "givenName": name.split()[0] if " " in name else name,
            "familyName": name.split()[-1] if " " in name else "",
        },
        "emails": [{"value": email, "type": "work", "primary": True}],
        "active": active,
        "roles": [{"value": role, "primary": True}],
        "meta": {
            "resourceType": "User",
            "created": created.isoformat() if hasattr(created, "isoformat") else str(created),
            "lastModified": modified.isoformat() if hasattr(modified, "isoformat") else str(modified),
        },
    }


def list_users(engine: Engine, tenant: str, start_index: int = 1, count: int = 100,
               filter_str: Optional[str] = None) -> dict:
    """List users in SCIM 2.0 format with pagination and simple filter support."""
    t = store.users
    cond = [t.c.tenant == tenant]
    if filter_str:
        # Simple parser for userName eq "email@..."
        m = re.search(r'userName\s+eq\s+"([^"]+)"', filter_str, re.IGNORECASE)
        if m:
            cond.append(t.c.email == m.group(1))

    with engine.connect() as conn:
        all_rows = [dict(r._mapping) for r in conn.execute(select(t).where(and_(*cond)).order_by(t.c.created_at))]

    total = len(all_rows)
    # 1-based start_index per SCIM spec
    offset = max(0, start_index - 1)
    page = all_rows[offset:offset + count]

    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(page),
        "Resources": [scim_user_representation(r) for r in page],
    }


def get_user(engine: Engine, tenant: str, user_id: str) -> Optional[dict]:
    t = store.users
    with engine.connect() as conn:
        row = conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.id == user_id))).first()
        if not row:
            return None
        return scim_user_representation(dict(row._mapping))


def create_user(engine: Engine, tenant: str, data: dict) -> dict:
    """Create and provision a new user via SCIM 2.0."""
    emails = data.get("emails") or []
    email = data.get("userName") or (emails[0].get("value") if emails else "")
    if not email:
        raise ValueError("userName or email is required")

    name_obj = data.get("name") or {}
    name = name_obj.get("formatted") or " ".join(filter(None, [name_obj.get("givenName"), name_obj.get("familyName")])) or email
    active = data.get("active", True)
    roles = data.get("roles") or []
    role = (roles[0].get("value") if roles else None) or "read"

    user_id = data.get("id") or hashlib.sha256(f"scim:{tenant}:{email}".encode()).hexdigest()[:16]
    now = datetime.utcnow()
    t = store.users

    with engine.begin() as conn:
        existing = conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.id == user_id))).first()
        row = dict(
            id=user_id,
            issuer="scim",
            subject=email,
            tenant=tenant,
            email=email,
            name=name,
            sensitive=False,
            claims_role=role,
            role=role,
            disabled=not active,
            created_at=now,
            last_login_at=now,
        )
        if existing:
            conn.execute(t.update().where(t.c.id == user_id).values(**row))
        else:
            conn.execute(t.insert().values(**row))

    return scim_user_representation(row)


def update_user(engine: Engine, tenant: str, user_id: str, data: dict) -> Optional[dict]:
    """Update or deprovision an existing user via SCIM 2.0 PUT/PATCH."""
    t = store.users
    now = datetime.utcnow()
    with engine.begin() as conn:
        existing = conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.id == user_id))).first()
        if not existing:
            return None
        values: Dict[str, Any] = {"last_login_at": now}
        if "active" in data:
            values["disabled"] = not bool(data["active"])
        if "userName" in data or "emails" in data:
            emails = data.get("emails") or []
            values["email"] = data.get("userName") or (emails[0].get("value") if emails else existing.email)
        if "name" in data:
            n = data["name"]
            values["name"] = n.get("formatted") or " ".join(filter(None, [n.get("givenName"), n.get("familyName")])) or existing.name
        if "roles" in data:
            roles = data["roles"] or []
            if roles:
                values["role"] = roles[0].get("value", "read")

        conn.execute(t.update().where(and_(t.c.tenant == tenant, t.c.id == user_id)).values(**values))
        updated = conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.id == user_id))).first()
        return scim_user_representation(dict(updated._mapping))


def delete_user(engine: Engine, tenant: str, user_id: str) -> bool:
    """Deprovision (disable) a user upon SCIM DELETE."""
    t = store.users
    with engine.begin() as conn:
        res = conn.execute(t.update().where(and_(t.c.tenant == tenant, t.c.id == user_id)).values(disabled=True))
        return res.rowcount > 0


def service_provider_config() -> dict:
    """Standard SCIM 2.0 ServiceProviderConfig descriptor."""
    return {
        "schemas": [SCIM_SP_CONFIG_SCHEMA],
        "documentationUri": "https://assay.dev/docs/sso",
        "patch": {"supported": True},
        "bulk": {"supported": False},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "name": "OAuth Bearer Token",
                "description": "Authentication scheme using the Authorization header",
                "specUri": "http://www.rfc-editor.org/info/rfc6750",
                "type": "oauthbearertoken",
                "primary": True,
            }
        ],
    }


# ---------- SAML 2.0 ----------

def saml_sp_metadata(entity_id: str, acs_url: str) -> str:
    """Generate SAML 2.0 Service Provider Metadata XML."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{entity_id}">\n'
        f'  <md:SPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">\n'
        f'    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>\n'
        f'    <md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location="{acs_url}" index="1" isDefault="true"/>\n'
        f'  </md:SPSSODescriptor>\n'
        f'</md:EntityDescriptor>'
    )


def parse_saml_response(saml_response_b64: str) -> dict:
    """Decode and parse a SAML 2.0 assertion XML payload."""
    try:
        xml_bytes = base64.b64decode(saml_response_b64)
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        raise ValueError(f"Invalid SAMLResponse: {exc}")

    # Namespace map
    ns = {
        "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
        "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    }

    # Extract NameID / subject
    name_id_node = root.find(".//saml:Subject/saml:NameID", ns)
    name_id = name_id_node.text.strip() if name_id_node is not None and name_id_node.text else ""

    # Extract attributes
    attributes: Dict[str, Any] = {}
    for attr in root.findall(".//saml:AttributeStatement/saml:Attribute", ns):
        attr_name = attr.get("Name") or ""
        vals = [v.text.strip() for v in attr.findall("saml:AttributeValue", ns) if v.text]
        if vals:
            attributes[attr_name] = vals[0] if len(vals) == 1 else vals

    email = name_id or attributes.get("email") or attributes.get("User.Email") or attributes.get("Email")
    if not email:
        raise ValueError("No NameID or email found in SAML assertion")

    name = attributes.get("displayName") or attributes.get("User.DisplayName") or attributes.get("name") or email
    roles = attributes.get("groups") or attributes.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]

    return {
        "subject": name_id or email,
        "email": email,
        "name": name,
        "roles": roles,
        "attributes": attributes,
    }
