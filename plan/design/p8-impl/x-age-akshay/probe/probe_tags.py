"""X-AGE B-2 probe: never/always tags in bringup.yml, and copy validate: with sha256.

Usage: python probe_tags.py <probe-dir>. Works only inside <probe-dir> (a copy of playbooks/ at
<probe-dir>/pb); never deletes anything. Uses the playbook test harness's Lab for the fake lab
and its inventory.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

D = Path(sys.argv[1])
XAGE = Path.home() / "x-age"
sys.path.insert(0, str(XAGE))
from tests.test_playbooks import Lab  # noqa: E402

PB = D / "pb"

# --- 1. the draft bringup.yml edits (B-2) and a stub install_age.yml ----------------------
bring = (PB / "bringup.yml").read_text()
old = "      ansible.builtin.import_tasks: tasks/resolve.yml\n"
assert bring.count(old) == 1
bring = bring.replace(old, old + "      tags: [always]\n")
report = "- name: report\n"
assert bring.count(report) == 1
bring = bring.replace(report, (
    "- name: age v1.1.1 on the DPU (only with --tags age)\n"
    "  hosts: r_dpu\n"
    "  gather_facts: false\n"
    "  tasks:\n"
    "    - name: install age\n"
    "      ansible.builtin.import_tasks: tasks/install_age.yml\n"
    "      tags: [age, never]\n\n") + report)
(PB / "bringup.yml").write_text(bring)
(PB / "tasks" / "install_age.yml").write_text(
    "- name: AGE-STUB controller step\n"
    "  ansible.builtin.debug: {msg: controller}\n"
    "  delegate_to: localhost\n"
    "  run_once: true\n"
    "- name: AGE-STUB dpu step\n"
    "  ansible.builtin.debug: {msg: \"dpu {{ inventory_hostname }}\"}\n")

# --- 2. validate: probe playbook -----------------------------------------------------------
vdir = D / "validate"
vdir.mkdir(exist_ok=True)
(vdir / "good.bin").write_bytes(b"pinned binary\n")
(vdir / "bad.bin").write_bytes(b"tampered binary\n")
pin = hashlib.sha256(b"pinned binary\n").hexdigest()
(vdir / "validate.yml").write_text(
    "- hosts: localhost\n"
    "  gather_facts: false\n"
    "  tasks:\n"
    "    - name: VALIDATE-COPY\n"
    "      ansible.builtin.copy:\n"
    "        src: \"{{ src_ }}\"\n"
    "        dest: \"{{ dest_ }}\"\n"
    "        mode: \"0755\"\n"
    "        validate: \"sh -c 'echo \\\"{{ want }}  $1\\\" | sha256sum -c --status' sh %s\"\n")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]


def show(title, p, needles):
    out = p.stdout + p.stderr
    print(f"  -- {title}: rc={p.returncode}")
    for line in out.splitlines():
        if any(n in line for n in needles):
            print("     " + line.rstrip()[:170])


for venv in (".venv", ".venv-220"):
    ap = str(XAGE / venv / "bin" / "ansible-playbook")
    ver = subprocess.run([ap, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    print(f"## {venv}: {ver}")
    labdir = D / f"lab{venv}"
    labdir.mkdir(exist_ok=True)
    lab = Lab(labdir)
    st = lab.state()
    st["installed"] = ["dpdk-devbind.py"]
    st["units"]["nginx-tls.service"] = "active"
    lab.set(st)
    ev = {"setup": "tb-01", "daolab_members": [{"name": "dpu-01", "kind": "dpu", "host": "tb-01-xcp"},
                                               {"name": "tg-01", "kind": "nic", "host": "tb-01-xcp"}],
          "ansible_become": False, "daolab_bringup_root": str(labdir / "root"),
          "hw_class": "sdp-pf1-vf8", "daolab_deploy_vms": False, "daolab_xo": "daolab-xo"}
    evf = labdir / "ev.json"
    evf.write_text(json.dumps(ev))
    env = dict(os.environ)
    env.update({"PATH": f"{lab.bindir}:{env['PATH']}", "FAKELAB_STATE": str(lab.state_path),
                "FAKELAB_LOG": str(lab.log_path), "FAKELAB_UNIT_DIR": str(labdir),
                "ANSIBLE_CONFIG": str(PB / "ansible.cfg"), "ANSIBLE_NOCOLOR": "1",
                "ANSIBLE_LOCAL_TEMP": str(labdir / "tmp"), "ANSIBLE_HOME": str(labdir / "ah")})
    base = [ap, "-i", str(lab.inventory), str(PB / "bringup.yml"), "-e", f"@{evf}"]

    def run(*extra, envx=None):
        e = dict(env, **(envx or {}))
        return subprocess.run(base + list(extra), env=e, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=300)

    keys = ("play #", "AGE-STUB", "resolve", "add the DPU", "TAGS")
    show("(a) --list-tasks, no tags", run("--list-tasks"), keys)
    show("(b1) --list-tasks --tags age", run("--list-tasks", "--tags", "age"), keys)
    p = run("--tags", "age")
    show("(b2) run --tags age", p, ("TASK [", "ok: [", "changed: [", "skipping: no hosts", "fatal", "PLAY ["))
    p = run(envx={"ANSIBLE_RUN_TAGS": "age"})
    print(f"  -- (b3) run with ANSIBLE_RUN_TAGS=age (the harness way): rc={p.returncode}, "
          f"'dpu dpu-01' in output: {'dpu dpu-01' in p.stdout}")
    p = run()
    print(f"  -- (a2) run with no tags: rc={p.returncode}, 'AGE-STUB' in output: {'AGE-STUB' in p.stdout}, "
          f"'ready: nothing missing' in output: {'ready: nothing missing' in p.stdout}")

    # (c) validate
    vb = [ap, "-i", "localhost,", "-c", "local", str(vdir / "validate.yml")]
    venv_tag = venv.strip(".")
    tgt = vdir / f"target-{venv_tag}"
    tgt.write_bytes(b"previous installed binary\n")
    before = sha(tgt)
    p = subprocess.run(vb + ["-e", f"src_={vdir / 'bad.bin'}", "-e", f"dest_={tgt}", "-e", f"want={pin}"],
                       env=env, capture_output=True, text=True, timeout=300)
    print(f"  -- (c1) copy bad.bin over an existing target, pin = good.bin's sha: rc={p.returncode}; "
          f"target sha before {before} after {sha(tgt)} unchanged={before == sha(tgt)}")
    for line in (p.stdout + p.stderr).splitlines():
        if "fatal" in line or "failed validation" in line:
            print("     " + line.rstrip()[:200])
    p = subprocess.run(vb + ["-e", f"src_={vdir / 'good.bin'}", "-e", f"dest_={tgt}", "-e", f"want={pin}"],
                       env=env, capture_output=True, text=True, timeout=300)
    print(f"  -- (c2) copy good.bin: rc={p.returncode}; target now == good.bin: "
          f"{sha(tgt) == sha(vdir / 'good.bin')}; mode {oct(tgt.stat().st_mode & 0o777)}")
    missing = vdir / f"fresh-{venv_tag}"
    p = subprocess.run(vb + ["-e", f"src_={vdir / 'bad.bin'}", "-e", f"dest_={missing}", "-e", f"want={pin}"],
                       env=env, capture_output=True, text=True, timeout=300)
    print(f"  -- (c3) copy bad.bin to a new path: rc={p.returncode}; target created: {missing.exists()}")
