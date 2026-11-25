#!/usr/bin/python3
"""vm: run commands in, copy files to/from, and get a shell on hv1's VMs through the QEMU guest
agent (no SSH or network path into the VM needed). Managed by homelab (role vm_tools).

  vm list                                  VMs, state, guest agent
  vm exec [-n] [-u USER] [-t SECS] VM CMD...  run as root (or USER); piped stdin is passed
                                           through (like ssh; -n ignores it, e.g. in loops)
  vm shell [-u USER] VM                    line-by-line pseudo-shell (keeps cwd; no TTY programs)
  vm console VM                            real login on the serial console (Ctrl-] to leave)
  vm push VM LOCAL REMOTE                  copy a file into the VM
  vm pull VM REMOTE LOCAL                  copy a file out of the VM

Exit status of `exec` is the command's own. Output is captured by the agent and printed when the
command ends, so long-running commands show nothing until they finish (use `console` for those).
"""
import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
import time

URI = os.environ.get("VM_LIBVIRT_URI", "qemu:///system")
CHUNK = 48 * 1024


class AgentError(Exception):
    pass


def virsh(*args, check=True):
    r = subprocess.run(["virsh", "-q", "-c", URI, *args], capture_output=True, text=True)
    if check and r.returncode:
        raise AgentError(r.stderr.strip() or f"virsh {args[0]} failed")
    return r.stdout


def agent(vm, command, timeout=15, **arguments):
    msg = {"execute": command}
    if arguments:
        msg["arguments"] = arguments
    out = virsh("qemu-agent-command", vm, json.dumps(msg), "--timeout", str(timeout))
    return json.loads(out).get("return")


def start(vm, argv, user=None, stdin_data=None, cwd=None):
    script = shlex.join(argv) if len(argv) > 1 else argv[0]
    if cwd:
        script = f"cd {shlex.quote(cwd)} && {{ {script}\n}}"
    if user and user != "root":
        path, args = "/usr/sbin/runuser", ["-u", user, "--", "/bin/bash", "-lc", script]
    else:
        path, args = "/bin/bash", ["-c", script]
    a = {"path": path, "arg": args, "capture-output": True}
    if stdin_data is not None:
        a["input-data"] = base64.b64encode(stdin_data).decode()
    return agent(vm, "guest-exec", **a)["pid"]


def wait(vm, pid, timeout):
    deadline, delay = time.monotonic() + timeout, 0.05
    while True:
        st = agent(vm, "guest-exec-status", pid=pid)
        if st.get("exited"):
            return st
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(delay)
        delay = min(delay * 1.5, 1.0)


def kill(vm, pid):
    try:
        agent(vm, "guest-exec", path="/bin/kill", arg=["-TERM", str(pid)])
    except AgentError:
        pass


def run(vm, argv, user=None, timeout=600, stdin_data=None, cwd=None):
    pid = start(vm, argv, user, stdin_data, cwd)
    try:
        st = wait(vm, pid, timeout)
    except (TimeoutError, KeyboardInterrupt) as e:
        kill(vm, pid)
        print(f"vm: {'timed out' if isinstance(e, TimeoutError) else 'interrupted'}; sent SIGTERM to "
              f"pid {pid} in {vm}", file=sys.stderr)
        return 124 if isinstance(e, TimeoutError) else 130, b"", b""
    out = base64.b64decode(st.get("out-data", ""))
    err = base64.b64decode(st.get("err-data", ""))
    if st.get("out-truncated") or st.get("err-truncated"):
        err += b"\nvm: output truncated by the guest agent\n"
    code = st.get("exitcode", 128 + st.get("signal", 0))
    return code, out, err


# ---------- commands ----------

def cmd_list(_):
    names = virsh("list", "--all", "--name").split()
    for n in names:
        state = virsh("domstate", n, check=False).strip()
        ga = "-"
        if state == "running":
            try:
                agent(n, "guest-ping", timeout=3)
                ga = "ok"
            except AgentError:
                ga = "no agent"
        print(f"{n:10} {state:12} {ga}")
    return 0


def cmd_exec(a):
    stdin_data = None if (a.no_stdin or sys.stdin.isatty()) else sys.stdin.buffer.read()
    code, out, err = run(a.vm, a.cmd, a.user, a.timeout, stdin_data)
    sys.stdout.buffer.write(out)
    sys.stderr.buffer.write(err)
    return code


def cmd_shell(a):
    try:
        import readline  # noqa: F401  (line editing + history)
    except ImportError:
        pass
    user = a.user or "root"
    code, out, _ = run(a.vm, ["cd ~ && pwd"], user)
    cwd = out.decode().strip() or "/"
    print(f"vm shell on {a.vm} as {user}. Each line runs on its own (cd is remembered, other state "
          f"isn't); no interactive programs. Ctrl-D to leave.", file=sys.stderr)
    last = 0
    while True:
        try:
            line = input(f"{user}@{a.vm}:{cwd}{'#' if user == 'root' else '$'} ")
        except EOFError:
            print()
            return last
        except KeyboardInterrupt:
            print()
            continue
        if not line.strip():
            continue
        marker = "__VM_PWD__"
        code, out, err = run(a.vm, [f"{line}\n__rc=$?; printf '\\n{marker}%s' \"$PWD\"; exit $__rc"],
                             user, a.timeout, cwd=cwd)
        text = out.decode(errors="replace")
        if marker in text:
            text, new_cwd = text.rsplit(marker, 1)
            cwd = new_cwd or cwd
            text = text[:-1] if text.endswith("\n") else text
        sys.stdout.write(text)
        sys.stderr.write(err.decode(errors="replace"))
        sys.stdout.flush()
        last = code
        if code:
            print(f"[exit {code}]", file=sys.stderr)


def cmd_console(a):
    print("Serial console: press Enter for a login prompt; Ctrl-] to leave.", file=sys.stderr)
    os.execvp("virsh", ["virsh", "-c", URI, "console", a.vm])


def cmd_push(a):
    with open(a.local, "rb") as f:
        data = f.read()
    h = agent(a.vm, "guest-file-open", path=a.remote, mode="wb")
    try:
        for i in range(0, len(data), CHUNK) or [0]:
            agent(a.vm, "guest-file-write", handle=h, **{"buf-b64": base64.b64encode(data[i:i + CHUNK]).decode()})
    finally:
        agent(a.vm, "guest-file-close", handle=h)
    print(f"{len(data)} bytes -> {a.vm}:{a.remote} (owner root; fix with vm exec chown/chmod)", file=sys.stderr)
    return 0


def cmd_pull(a):
    h = agent(a.vm, "guest-file-open", path=a.remote, mode="rb")
    data = b""
    try:
        while True:
            r = agent(a.vm, "guest-file-read", handle=h, count=CHUNK)
            data += base64.b64decode(r.get("buf-b64", ""))
            if r.get("eof") or not r.get("count"):
                break
    finally:
        agent(a.vm, "guest-file-close", handle=h)
    if a.local == "-":
        sys.stdout.buffer.write(data)
    else:
        with open(a.local, "wb") as f:
            f.write(data)
        print(f"{a.vm}:{a.remote} -> {a.local} ({len(data)} bytes)", file=sys.stderr)
    return 0


def main():
    p = argparse.ArgumentParser(prog="vm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    e = sub.add_parser("exec")
    e.add_argument("-n", "--no-stdin", action="store_true")
    e.add_argument("-u", "--user")
    e.add_argument("-t", "--timeout", type=float, default=600)
    e.add_argument("vm")
    e.add_argument("cmd", nargs=argparse.REMAINDER)
    e.set_defaults(fn=cmd_exec)
    s = sub.add_parser("shell")
    s.add_argument("-u", "--user")
    s.add_argument("-t", "--timeout", type=float, default=600)
    s.add_argument("vm")
    s.set_defaults(fn=cmd_shell)
    c = sub.add_parser("console")
    c.add_argument("vm")
    c.set_defaults(fn=cmd_console)
    for name, fn, a1, a2 in (("push", cmd_push, "local", "remote"), ("pull", cmd_pull, "remote", "local")):
        x = sub.add_parser(name)
        x.add_argument("vm")
        x.add_argument(a1)
        x.add_argument(a2)
        x.set_defaults(fn=fn)
    a = p.parse_args()
    if a.action == "exec":
        if a.cmd[:1] == ["--"]:
            a.cmd = a.cmd[1:]
        if not a.cmd:
            p.error("exec needs a command")
    try:
        return a.fn(a)
    except AgentError as ex:
        print(f"vm: {ex}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    sys.exit(main())
