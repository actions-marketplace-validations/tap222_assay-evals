import base64
import pytest
from fastapi.testclient import TestClient
from assay import auth, store
from assay.api import create_app
from assay.config import Settings

ADMIN = "platform-admin-secret"


@pytest.fixture
def client(tmp_path):
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'scim.db'}", admin_key=ADMIN)))
    return c


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def test_scim_endpoints_crud(client):
    headers = bearer(ADMIN)

    # 1. ServiceProviderConfig
    cfg = client.get("/scim/v2/ServiceProviderConfig", headers=headers)
    assert cfg.status_code == 200
    assert "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig" in cfg.json()["schemas"]

    # 2. Create User
    new_user = {
        "userName": "employee@acme.corp",
        "name": {"givenName": "Jane", "familyName": "Doe"},
        "active": True,
        "roles": [{"value": "manage"}],
    }
    res = client.post("/scim/v2/Users", headers=headers, json=new_user)
    assert res.status_code == 201
    created = res.json()
    assert created["userName"] == "employee@acme.corp"
    assert created["active"] is True
    uid = created["id"]

    # 3. Get User
    get_res = client.get(f"/scim/v2/Users/{uid}", headers=headers)
    assert get_res.status_code == 200
    assert get_res.json()["id"] == uid

    # 4. List Users
    list_res = client.get("/scim/v2/Users", headers=headers)
    assert list_res.status_code == 200
    data = list_res.json()
    assert data["totalResults"] >= 1
    assert any(u["id"] == uid for u in data["Resources"])

    # 5. Patch / Update User (deprovision: active = False)
    patch_res = client.patch(f"/scim/v2/Users/{uid}", headers=headers, json={"active": False})
    assert patch_res.status_code == 200
    assert patch_res.json()["active"] is False

    # 6. Delete User
    del_res = client.delete(f"/scim/v2/Users/{uid}", headers=headers)
    assert del_res.status_code == 204


def test_saml_metadata_and_acs(client):
    # Metadata XML
    meta = client.get("/v1/auth/saml/metadata")
    assert meta.status_code == 200
    assert meta.headers["content-type"] == "application/xml"
    assert "EntityDescriptor" in meta.text
    assert "AssertionConsumerService" in meta.text

    # Simulated SAML 2.0 Response XML
    saml_xml = (
        '<?xml version="1.0"?>\n'
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">\n'
        '  <saml:Assertion>\n'
        '    <saml:Subject>\n'
        '      <saml:NameID>saml_user@acme.corp</saml:NameID>\n'
        '    </saml:Subject>\n'
        '    <saml:AttributeStatement>\n'
        '      <saml:Attribute Name="displayName">\n'
        '        <saml:AttributeValue>Sam L. User</saml:AttributeValue>\n'
        '      </saml:Attribute>\n'
        '      <saml:Attribute Name="groups">\n'
        '        <saml:AttributeValue>admin</saml:AttributeValue>\n'
        '      </saml:Attribute>\n'
        '    </saml:AttributeStatement>\n'
        '  </saml:Assertion>\n'
        '</samlp:Response>'
    )
    b64_xml = base64.b64encode(saml_xml.encode()).decode()

    # ACS post
    acs_res = client.post("/v1/auth/saml/acs", data={"SAMLResponse": b64_xml})
    assert acs_res.status_code == 200
    body = acs_res.json()
    assert body["authenticated"] is True
    assert body["user"]["userName"] == "saml_user@acme.corp"
