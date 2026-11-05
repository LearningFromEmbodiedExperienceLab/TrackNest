from concurrent.futures import ThreadPoolExecutor
import hashlib
import re
import time
from urllib.parse import urlsplit, parse_qs

from app import app, db, TrackioUser, UserInvitation, SharedProject
from conftest import login, post


def create_invitation(admin, username):
    response = post(admin, "/add", username=username)
    assert response.status_code == 200
    assert 'id="copy-invitation"' in response.text
    assert 'href="/admin/users"' in response.text
    assert 'id="new-username"' not in response.text
    assert username in response.text
    url = re.search(r'id="invitation-url"[^>]*value="([^"]+)"', response.text)[1]
    return parse_qs(urlsplit(url).fragment)["invite"][0]


def submit_registration(client, token, password="registered-password-123"):
    client.get("/register")
    return post(client, "/register", token=token, password=password, confirm_password=password)


def test_administrator_and_users_have_their_own_panel(users):
    for username in ("admin", "alice", "bob"):
        client = app.test_client()
        login(client, username)
        page = client.get("/")
        assert "我的工作空间" in page.text
        assert 'id="project-name"' in page.text
        assert 'id="new-username"' not in page.text
        assert (client.get("/admin/users").status_code == 200) == (username == "admin")
        for name in users:
            if name != username:
                assert f"/stop/{users[name].id}" not in page.text
                assert f"/start/{users[name].id}" not in page.text


def test_registration_invitation_lifecycle(users):
    admin = app.test_client()
    login(admin, "admin")
    token = create_invitation(admin, "new-user")
    user = TrackioUser.query.filter_by(username="new-user").one()
    invitation = db.session.get(UserInvitation, user.id)
    assert user.password_hash is None
    assert invitation.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert invitation.token_hash != token
    assert 6 * 86400 < invitation.expires_at - time.time() <= 7 * 86400
    assert token not in admin.get("/admin/users").text
    guest = app.test_client()
    guest.get("/register")
    assert post(guest, "/register/inspect", token=token).json == {"username": "new-user"}
    assert post(guest, "/login", username="new-user", password="registered-password-123").status_code == 401
    assert submit_registration(guest, token, password="short").status_code == 400
    assert submit_registration(guest, token).status_code == 302
    assert db.session.get(UserInvitation, user.id) is None
    assert submit_registration(guest, token).status_code == 400
    assert post(admin, f"/admin/users/{user.id}/invitation").status_code == 400
    assert post(guest, "/login", username="new-user", password="registered-password-123").status_code == 302
    assert guest.get("/").status_code == 200


def test_renew_expire_delete_and_registration_security(users):
    admin = app.test_client()
    login(admin, "admin")
    old = create_invitation(admin, "pending")
    user = TrackioUser.query.filter_by(username="pending").one()
    response = post(admin, f"/admin/users/{user.id}/invitation")
    assert 'id="copy-invitation"' in response.text
    token = parse_qs(urlsplit(re.search(r'id="invitation-url"[^>]*value="([^"]+)"', response.text)[1]).fragment)["invite"][0]
    assert token != old
    guest = app.test_client()
    assert submit_registration(guest, old).status_code == 400
    assert guest.post("/register", data={"token": token}).status_code == 400
    guest.get("/register")
    assert post(guest, "/register", token=token, password="registered-password-123", confirm_password="different").status_code == 400
    invitation = db.session.get(UserInvitation, user.id)
    invitation.expires_at = int(time.time()) - 1
    db.session.commit()
    assert submit_registration(guest, token).status_code == 400
    assert post(admin, f"/delete/{user.id}").status_code == 302
    assert db.session.get(UserInvitation, user.id) is None
    assert submit_registration(guest, token).status_code == 400
    login(guest, "alice")
    assert post(guest, "/add", username="unauthorized").status_code == 403
    assert post(guest, f"/admin/users/{users['bob'].id}/invitation").status_code == 403


def test_pending_accounts_cannot_be_project_members(users):
    admin = app.test_client()
    login(admin, "admin")
    create_invitation(admin, "pending")
    client = app.test_client()
    login(client, "alice")
    post(client, "/projects", name="private-project")
    project = SharedProject.query.one()
    page = client.get(f"/projects/{project.id}")
    candidates = re.search(r'<select id="member-username"[^>]*>(.*?)</select>', page.text, re.S)[1]
    assert 'value="bob"' in candidates and 'value="pending"' not in candidates
    assert post(client, f"/projects/{project.id}/members", username="pending", role="viewer").status_code == 400


def test_registration_link_is_single_use_under_concurrent_requests(users):
    admin = app.test_client()
    login(admin, "admin")
    token = create_invitation(admin, "concurrent")
    def register_once(_):
        with app.test_client() as guest:
            return submit_registration(guest, token).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(register_once, range(2))) == [302, 400]


def test_registration_language_switch_preserves_invitation(users):
    admin = app.test_client()
    login(admin, "admin")
    token = create_invitation(admin, "language-user")
    guest = app.test_client()
    guest.get("/register")
    destination = "/register#invite=" + token
    response = post(guest, "/language", language="en", next=destination)
    assert response.location == destination
    assert b"Complete your registration" in guest.get("/register").data
