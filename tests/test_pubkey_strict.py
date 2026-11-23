"""P-0 (F-1): user-controlled text must never reach ansible as a Jinja expression.

Two inputs were found to be re-templated on the controller by real ansible-playbook:
  * the SSH public-key comment (`dpu_access` role, tasks/main.yml), and
  * the values of `templates` of a diy reservation (`tasks/plan_vms.yml`).
Both are closed three ways here: the API refuses them (422), the add-user CLI refuses them, and the
context the engine hands to the runner is scrubbed, so a row stored *before* the fix cannot reach
ansible-runner either. The canary tests run the real playbook code and look for a file the payload
would create; the payload only ever `touch`es a file inside pytest's tmp_path.
"""
import argparse
import dataclasses
import json
import os
import subprocess

import pytest

from service import cli, config, models, runner
from tests.test_deploy_access import KEY, deploy
from tests.test_playbooks import ANSIBLE, REPO, Lab, access_play

KEY2 = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQ== bob@laptop"
TPL = "tpl-ubuntu2204-dao"


def _tpls(api_env, value=TPL):
    """One template per VM host of tb-01 (the API wants exactly the setup's hosts); the first
    host carries `value`, the others the normal template."""
    sid = models.setup_by_name(api_env.conn, "tb-01")["id"]
    hosts = sorted(models.setup_vm_hosts(api_env.conn, sid))
    return {h: (value if i == 0 else TPL) for i, h in enumerate(hosts)}


def pipe_payload(canary) -> str:
    return "{{ lookup('pipe', 'touch " + str(canary) + "') }}"


# ---- the parser (unit) ----------------------------------------------------------------------

GOOD_LINES = [
    KEY, KEY2,
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDeploy+Key/0",                       # no comment
    "ssh-ed25519 AAAA test", "ssh-rsa AAAAwork alice@work", "ssh-ed25519 AAAAnew alice@new",
    "ecdsa-sha2-nistp256 AAAAE2VjZHNh=", "ecdsa-sha2-nistp384 AAAAE2VjZHNh==",
    "ecdsa-sha2-nistp521 AAAAE2VjZHNh laptop (work), 2026",
    "sk-ssh-ed25519@openssh.com AAAAGnNr yubikey", "sk-ecdsa-sha2-nistp256@openssh.com AAAAInNr yk",
]

BAD_COMMENTS = [
    "{{ 7*7 }}", "{% if 1 %}x{% endif %}", "{# c #}", "{", "}", "a{b", "x}y",
    "{{ lookup('pipe', 'id') }}", "$(id)", "${HOME}", "`id`", "a;b", "a|b", "a&b", "a<b", "a>b",
    'a"b', "a'b", "a\\b", "a%b", "a#b", "a*b", "a?b", "a[b", "a~b", "a!b",
    "\x7f", "a\x7fb", "\x85", "a\x85b", " ", "a b", " ", "a b",
    "é", "名前", "a b", "a​b", "x" * 129,
] + [f"a{chr(c)}b" for c in range(0x20) if chr(c) not in "\n"] + ["a\nb"]


@pytest.mark.parametrize("line", GOOD_LINES)
def test_a_good_key_line_is_accepted(line):
    assert models.ssh_key_line_ok(line)


@pytest.mark.parametrize("comment", BAD_COMMENTS)
def test_an_unsafe_comment_is_refused(comment):
    assert not models.ssh_key_line_ok(f"{KEY.rsplit(' ', 1)[0]} {comment}")


@pytest.mark.parametrize("line", [KEY + "\n", KEY + "\r", KEY + "\r\n", KEY + "\x85", KEY + " ",
                                  KEY + " ", KEY + "\x00", "\n" + KEY, " " + KEY, KEY + " ",
                                  KEY + "  ", "", " ", "ssh-ed25519", "ssh-ed25519 ", "not a key",
                                  "ssh-ed25519 AAA$$ x", 'command="sh" ' + KEY, "ssh-dss AAAA x"])
def test_the_line_parser_is_a_fullmatch(line):
    assert not models.ssh_key_line_ok(line)


def test_a_comment_of_128_characters_is_the_limit():
    assert models.ssh_key_line_ok(KEY.rsplit(" ", 1)[0] + " " + "x" * 128)


# ---- the API refuses them -----------------------------------------------------------------

API_BAD = [KEY.rsplit(" ", 1)[0] + " " + c for c in BAD_COMMENTS if "\n" not in c] + [
    KEY + "\rssh-rsa AAAAb x", KEY + "\x0bssh-rsa AAAAb x", KEY + "\x1cssh-rsa AAAAb x",
    KEY + "\x85ssh-rsa AAAAb x", KEY + " ssh-rsa AAAAb x", KEY + " ssh-rsa AAAAb x",
    KEY + "\x0cssh-rsa AAAAb x", KEY + "\x00", KEY + "\x7f",
    "\n".join([KEY] * 6), KEY + "\n" + KEY2 + "\n" + KEY.rsplit(" ", 1)[0] + " {{ 7*7 }}",
]


@pytest.mark.parametrize("bad", API_BAD)
def test_the_api_refuses_an_unsafe_key(api_env, bad):
    r = deploy(api_env, ssh_pubkey=bad)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text
    assert models.list_reservations(api_env.conn) == []


@pytest.mark.parametrize("blob,expect", [
    (KEY, [KEY]),
    (KEY + "\n", [KEY]),                                                # the CLI sends a file's text
    (KEY + "\r\n", [KEY]),
    (KEY + "\r\n" + KEY2 + "\r\n", [KEY, KEY2]),
    ("\n\n" + KEY + "\n\n" + KEY2 + "\n", [KEY, KEY2]),
    ("  " + KEY + "  \n\t" + KEY2 + "\t", [KEY, KEY2]),
    ("\n".join([KEY] * 5), [KEY] * 5),
])
def test_blob_level_newlines_still_work(api_env, blob, expect):
    r = deploy(api_env, ssh_pubkey=blob)
    assert r.status_code == 201, r.text
    row = models.reservation_by_id(api_env.conn, r.json()["id"])
    assert row["ssh_pubkey"] == "\n".join(expect)


@pytest.mark.parametrize("value", ["{{ 7*7 }}", "{% if 1 %}x{% endif %}", "{# c #}", "a{b", "tpl;id",
                                   "tpl$(id)", "tpl`id`", "tpl\n", "tpl\x00", "tpl\x7f", "tpl\x85",
                                   "tpl ", "tpl é", "", " tpl", "tpl ", "x" * 129,
                                   "{{ lookup('pipe', 'id') }}"])
def test_the_api_refuses_an_unsafe_diy_template(api_env, value):
    r = api_env.client.post("/reservations", headers={"Authorization": f"Bearer {api_env.tokens['alice']}"},
                            json={"usecase": "diy", "hours": 1, "setup": "tb-01",
                                  "templates": _tpls(api_env, value)})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text
    assert models.list_reservations(api_env.conn) == []


@pytest.mark.parametrize("value", [TPL, "Ubuntu Jammy Jellyfish 22.04", "tpl_a-b.c (v2)+x:1@h"])
def test_a_normal_diy_template_name_is_accepted(api_env, value):
    r = api_env.client.post("/reservations", headers={"Authorization": f"Bearer {api_env.tokens['alice']}"},
                            json={"usecase": "diy", "hours": 1, "setup": "tb-01",
                                  "templates": _tpls(api_env, value)})
    assert r.status_code == 201, r.text


# ---- the add-user CLI ----------------------------------------------------------------------

@pytest.fixture
def cli_db(monkeypatch, tmp_path):
    path = str(tmp_path / "daolab.db")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, db_path=path))
    conn = models.connect(path)
    models.init_db(conn)
    return conn


def _add_user(tmp_path, text, username="carol"):
    key = tmp_path / "k.pub"
    key.write_bytes(text.encode("utf-8"))
    return cli.cmd_add_user(argparse.Namespace(username=username, pubkey_path=str(key),
                                               role="user", rotate=False))


@pytest.mark.parametrize("text", [
    KEY.rsplit(" ", 1)[0] + " {{ lookup('pipe', 'id') }}\n", KEY + "\x7f\n", KEY + " x\n",
    KEY.rsplit(" ", 1)[0] + " é\n", "not a key\n", KEY + "\n" + KEY.rsplit(" ", 1)[0] + " {% x %}\n",
])
def test_add_user_refuses_an_unsafe_key(cli_db, tmp_path, capsys, text):
    with pytest.raises(SystemExit):
        _add_user(tmp_path, text)
    assert cli_db.execute("SELECT count(*) FROM users WHERE username = 'carol'").fetchone()[0] == 0


@pytest.mark.parametrize("text", [KEY + "\n", KEY + "\r\n", KEY + "\n" + KEY2 + "\n",
                                  "# laptop\n" + KEY + "\n\n" + KEY2 + "\n"])
def test_add_user_takes_a_normal_key_file(cli_db, tmp_path, capsys, text):
    assert _add_user(tmp_path, text) == 0
    assert cli_db.execute("SELECT count(*) FROM users WHERE username = 'carol'").fetchone()[0] == 1


@pytest.mark.parametrize("name", ["{{ 7*7 }}", "a{%b", "a{#b", "a\nb", "a\x7fb", "a b", ""])
def test_add_user_refuses_a_username_with_jinja_or_control_characters(cli_db, tmp_path, name):
    with pytest.raises(SystemExit):
        _add_user(tmp_path, KEY + "\n", username=name)


# ---- the chokepoint: rows stored BEFORE the fix cannot reach the runner -------------------------

def _diy(api_env, who="alice"):
    r = api_env.client.post("/reservations", headers={"Authorization": f"Bearer {api_env.tokens[who]}"},
                            json={"usecase": "diy", "hours": 1, "setup": "tb-01",
                                  "templates": _tpls(api_env)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _poison_key(api_env, rid, value):
    api_env.conn.execute("UPDATE reservations SET ssh_pubkey = ? WHERE id = ?", (value, rid))


def test_a_stored_poisoned_reservation_key_is_dropped_before_the_runner(api_env, tmp_path):
    rid = _diy(api_env)
    bad = KEY.rsplit(" ", 1)[0] + " " + pipe_payload(tmp_path / "canary")
    _poison_key(api_env, rid, KEY + "\n" + bad + "\n# " + pipe_payload(tmp_path / "canary") + "\n" + KEY2)
    ctx = models.reservation_context(api_env.conn, rid)
    assert ctx["ssh_pubkey"] == KEY + "\n" + KEY2
    ev = runner.AnsibleRunner(config.settings).extravars(ctx)
    assert "{{" not in json.dumps(ev["ssh_pubkey"]) and "lookup" not in json.dumps(ev["ssh_pubkey"])


def test_a_stored_poisoned_registered_key_is_dropped_before_the_runner(api_env, tmp_path):
    rid = _diy(api_env)
    bad = KEY.rsplit(" ", 1)[0] + " " + pipe_payload(tmp_path / "canary")
    uid = models.reservation_by_id(api_env.conn, rid)["user_id"]
    models.update(api_env.conn, "users", uid, {"ssh_pubkey": bad})
    api_env.conn.execute("UPDATE reservations SET ssh_pubkey = NULL WHERE id = ?", (rid,))
    ctx = models.reservation_context(api_env.conn, rid)
    assert ctx["ssh_pubkey"] == ""
    assert models.access_keys(models.reservation_by_id(api_env.conn, rid),
                              models.user_by_id(api_env.conn, uid)) == ""


def test_stored_poisoned_diy_templates_are_neutralised_before_the_runner(api_env, tmp_path):
    rid = _diy(api_env)
    payload = pipe_payload(tmp_path / "canary")
    api_env.conn.execute("UPDATE reservations SET diy_templates = ? WHERE id = ?",
                         (models.to_json(_tpls(api_env, payload)), rid))
    models.create_vms(api_env.conn, rid)
    api_env.conn.execute("UPDATE vms SET template = ? WHERE reservation_id = ? AND role = ?",
                         (payload, rid, sorted(_tpls(api_env))[0]))
    ctx = models.reservation_context(api_env.conn, rid)
    assert ctx["templates"] == _tpls(api_env, models.UNSAFE_TEMPLATE_NAME)
    assert sorted(v["template"] for v in ctx["vms"]) == sorted(_tpls(api_env, models.UNSAFE_TEMPLATE_NAME).values())
    ev = runner.AnsibleRunner(config.settings).extravars(ctx)
    assert "lookup" not in json.dumps(ev) and "{{" not in json.dumps(ev["templates"])


def test_a_clean_context_is_left_alone(api_env):
    rid = _diy(api_env)
    models.create_vms(api_env.conn, rid)
    ctx = models.reservation_context(api_env.conn, rid)
    assert ctx["templates"] == _tpls(api_env)
    assert [v["template"] for v in ctx["vms"]] == [TPL] * len(ctx["templates"])


# ---- the canary: real ansible code, a file the payload would create -----------------------------

needs_ansible = pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed")


@needs_ansible
def test_canary_diy_templates_never_template_in_the_real_plan_vms(api_env, tmp_path):
    canary = tmp_path / "canary-templates"
    rid = _diy(api_env)
    api_env.conn.execute("UPDATE reservations SET diy_templates = ? WHERE id = ?",
                         (models.to_json(_tpls(api_env, pipe_payload(canary))), rid))
    ev = runner.AnsibleRunner(config.settings).extravars(models.reservation_context(api_env.conn, rid))
    ev_path = tmp_path / "ev.json"
    ev_path.write_text(json.dumps(ev))
    (tmp_path / "wrap.yml").write_text(
        "- hosts: localhost\n  gather_facts: false\n  connection: local\n  tasks:\n"
        f"    - ansible.builtin.include_tasks: {REPO}/playbooks/tasks/plan_vms.yml\n")
    subprocess.run([ANSIBLE, "-i", "localhost,", str(tmp_path / "wrap.yml"), "-e", f"@{ev_path}"],
                   cwd=tmp_path, capture_output=True, timeout=300,
                   env={**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False", "ANSIBLE_NOCOLOR": "1"})
    assert not canary.exists(), "the diy template value was evaluated on the controller"


@needs_ansible
def test_canary_ssh_key_comment_never_templates_in_the_real_dpu_access_role(api_env, tmp_path):
    canary = tmp_path / "canary-key"
    rid = _diy(api_env)
    _poison_key(api_env, rid, KEY.rsplit(" ", 1)[0] + " " + pipe_payload(canary))
    ev = runner.AnsibleRunner(config.settings).extravars(models.reservation_context(api_env.conn, rid))
    (tmp_path / "lab").mkdir()
    lab = Lab(tmp_path / "lab")
    access_play(lab, ssh_pubkey=ev["ssh_pubkey"])
    assert not canary.exists(), "the SSH key comment was evaluated on the controller"
