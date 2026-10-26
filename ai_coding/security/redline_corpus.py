"""The red line corpus (M-D2): six classes, each verdict frozen by hand.

A benchmark over a *derived* corpus proves nothing: recompute the labels from
the code under test and the test can only agree with itself.  So every row
here is ``(command, label, verdict)`` where ``verdict`` is the string the
implementation answered **on the day the corpus was written** -- copied out of
a run, not inferred from the label.  The benchmark's hard assertion is that
comparison, which is why a verdict that moves is a red build and not a
footnote.

Six classes, and the split is deliberate:

* ``dangerous`` -- a literal red line.  Refused today, so an empty verdict is
  a **false negative**.
* ``unparseable_deny`` -- the grammar cannot read the line *and* a red line is
  in it, so the conservative fallback refuses.  An empty verdict is a **false
  negative**.
* ``unparseable_warn`` -- the grammar cannot read the line and there is no red
  line in it, so the line is only **asked about**.  The empty verdict is the
  existing contract (``tests/test_bash_red_lines.py`` pins it); counting it as
  a false negative would turn the CI red on day one.
* ``safe`` -- ordinary work, or text that only looks like a red line.  A
  non-empty verdict is a **false positive**.
* ``known_miss`` -- the grammar reads the line, no rule matches, and
  ``_grammar_reads`` then short-circuits the text fallback, so a command that
  *should* be refused is waved through.  Reported, never failed.
* ``known_fp`` -- business-safe work the rules still refuse (``chmod 777
  ./build``).  Reported, never failed.

**The ``unparseable`` split is the one that matters.**  Both halves are "the
grammar could not read this", and their verdicts differ -- ``echo "rm -rf /``
is refused, ``printf done &&`` is asked about.  Freezing the two as one class
was the defect this corpus exists to prevent.

``known_miss`` / ``known_fp`` are counted **from the verdict**, not from the
label, so a fix that closes a hole moves the number instead of leaving it
stuck above zero.  The re-freeze is part of the fix and is not optional: when a
``known_miss`` row is repaired, its ``verdict`` is changed to the new refusal
*and* its label moves to ``dangerous``.  The golden comparison forces that to
happen in the open.

This file reads no clock and no file; the frozen values are the point.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CORPUS",
    "DANGEROUS",
    "KNOWN_FP",
    "KNOWN_MISS",
    "MIN_DANGEROUS",
    "MIN_KNOWN_MISS",
    "MIN_SAFE",
    "MIN_UNPARSEABLE",
    "SAFE",
    "UNPARSEABLE_DENY",
    "UNPARSEABLE_WARN",
    "RedlineCase",
]


#: One row: the command, the class it belongs to, and the verdict frozen on
#: the day the corpus was written.
@dataclass(frozen=True)
class RedlineCase:
    command: str
    label: str
    verdict: str


#: Corpus floors.  A corpus thinned to nothing would make the benchmark pass
#: vacuously, so the sizes are asserted (here, in the script, and in the tests).
MIN_DANGEROUS = 20
MIN_UNPARSEABLE = 10
MIN_SAFE = 20
MIN_KNOWN_MISS = 5


#: Literal red lines, in every spelling the rules and the fallback agree on.
#: Sources: ``tests/test_bash_red_lines.py``, the roadmap's named commands
#: (``rm -rf /``, ``sudo``, ``dd if=``, ``> /dev/sd``, force push), and the
#: credential files the gate refuses to read.
DANGEROUS: tuple[RedlineCase, ...] = (
    RedlineCase("rm -rf /", "dangerous", "recursive root deletion (rm -rf /)"),
    RedlineCase("rm  -rf  /", "dangerous", "recursive root deletion (rm -rf /)"),
    RedlineCase("rm -r -f /", "dangerous", "recursive root deletion (rm -r -f /)"),
    RedlineCase("rm -fr /", "dangerous", "recursive root deletion (rm -fr /)"),
    RedlineCase("RM -RF /", "dangerous", "recursive root deletion (RM -RF /)"),
    RedlineCase(
        "rm --recursive --force /",
        "dangerous",
        "recursive root deletion (rm --recursive --force /)",
    ),
    RedlineCase("rm -rf /*", "dangerous", "recursive root deletion (rm -rf /*)"),
    RedlineCase("rm -rf /etc", "dangerous", "recursive root deletion (rm -rf /etc)"),
    RedlineCase("rm -rf /root", "dangerous", "recursive root deletion (rm -rf /root)"),
    RedlineCase("rm -rf /usr", "dangerous", "recursive root deletion (rm -rf /usr)"),
    RedlineCase("rm -rf /bin", "dangerous", "recursive root deletion (rm -rf /bin)"),
    RedlineCase("rm -rf /opt", "dangerous", "recursive root deletion (rm -rf /opt)"),
    RedlineCase("rm -rf /boot", "dangerous", "recursive root deletion (rm -rf /boot)"),
    RedlineCase("rm -rf /lib", "dangerous", "recursive root deletion (rm -rf /lib)"),
    RedlineCase(
        "chmod 777 /tmp/x", "dangerous", "world-writable permissions (chmod 777 /tmp/x)"
    ),
    RedlineCase(
        "chmod  777  /tmp/x",
        "dangerous",
        "world-writable permissions (chmod 777 /tmp/x)",
    ),
    RedlineCase(
        "chmod 0777 /tmp/x",
        "dangerous",
        "world-writable permissions (chmod 0777 /tmp/x)",
    ),
    RedlineCase(
        "chmod a+rwx /tmp/x",
        "dangerous",
        "world-writable permissions (chmod a+rwx /tmp/x)",
    ),
    RedlineCase(
        "chmod +rwx /tmp/x",
        "dangerous",
        "world-writable permissions (chmod +rwx /tmp/x)",
    ),
    RedlineCase(
        "chmod a+w /tmp/x", "dangerous", "world-writable permissions (chmod a+w /tmp/x)"
    ),
    RedlineCase("chmod 777", "dangerous", "world-writable permissions (chmod 777)"),
    RedlineCase("chmod 777 .", "dangerous", "world-writable permissions (chmod 777 .)"),
    RedlineCase("sudo rm x", "dangerous", "sudo escalation (sudo rm x)"),
    RedlineCase("sudo  ls", "dangerous", "sudo escalation (sudo ls)"),
    RedlineCase("sudo su -", "dangerous", "sudo escalation (sudo su -)"),
    RedlineCase("sudo -n true", "dangerous", "sudo escalation (sudo -n true)"),
    RedlineCase("sudo", "dangerous", "sudo escalation (sudo)"),
    RedlineCase("env sudo ls", "dangerous", "sudo escalation (env sudo ls)"),
    RedlineCase("nohup sudo ls", "dangerous", "sudo escalation (nohup sudo ls)"),
    RedlineCase("time sudo ls", "dangerous", "sudo escalation (time sudo ls)"),
    RedlineCase("command sudo ls", "dangerous", "sudo escalation (command sudo ls)"),
    RedlineCase("exec sudo ls", "dangerous", "sudo escalation (exec sudo ls)"),
    RedlineCase("shutdown now", "dangerous", "system shutdown (shutdown now)"),
    RedlineCase("reboot", "dangerous", "system reboot (reboot)"),
    RedlineCase("mkfs /dev/sda1", "dangerous", "filesystem format (mkfs /dev/sda1)"),
    RedlineCase(
        "mkfs.ext4 /dev/sda1", "dangerous", "filesystem format (mkfs.ext4 /dev/sda1)"
    ),
    RedlineCase(
        "dd if=/dev/zero of=/dev/sda",
        "dangerous",
        "disk write (dd if=/dev/zero of=/dev/sda)",
    ),
    RedlineCase(
        "dd of=/dev/sda if=/dev/zero",
        "dangerous",
        "disk write (dd of=/dev/sda if=/dev/zero)",
    ),
    RedlineCase(
        "dd if=/dev/sda of=/tmp/x",
        "dangerous",
        "disk write (dd if=/dev/sda of=/tmp/x)",
    ),
    RedlineCase("echo x > /dev/sda", "dangerous", "raw disk write (echo x)"),
    RedlineCase("echo x >/dev/sda", "dangerous", "raw disk write (echo x)"),
    RedlineCase(
        "git push --force origin main",
        "dangerous",
        "force git push (git push --force origin main)",
    ),
    RedlineCase("git push -f origin", "dangerous", "force git push (git push -f origin)"),
    RedlineCase("git push --force", "dangerous", "force git push (git push --force)"),
    RedlineCase("git push -f", "dangerous", "force git push (git push -f)"),
    RedlineCase(
        "git -C /tmp push --force",
        "dangerous",
        "force git push (git -C /tmp push --force)",
    ),
    RedlineCase("cat ~/.ssh/id_rsa", "dangerous", "credential file (id_rsa)"),
    RedlineCase("cat .env", "dangerous", "credential file (.env)"),
    RedlineCase("cat ~/.aws/credentials", "dangerous", "credential file (.aws)"),
    RedlineCase(
        "cat ~/.gnupg/secring.gpg", "dangerous", "credential file (.gnupg)"
    ),
    RedlineCase("cat ~/.ssh/config", "dangerous", "credential file (.ssh)"),
    RedlineCase("cat /etc/shadow", "dangerous", "credential file (etc/shadow)"),
    # A red line behind another command is still a red line: the grammar is the
    # unit, so where the command sits stopped mattering.
    RedlineCase("a && rm -rf /", "dangerous", "recursive root deletion (rm -rf /)"),
    RedlineCase("ls; rm -rf /", "dangerous", "recursive root deletion (rm -rf /)"),
    RedlineCase("ls | rm -rf /", "dangerous", "recursive root deletion (rm -rf /)"),
    RedlineCase("FOO=bar rm -rf /", "dangerous", "recursive root deletion (rm -rf /)"),
    RedlineCase("x=1; rm -rf /", "dangerous", "recursive root deletion (rm -rf /)"),
    RedlineCase("cat $(rm -rf /)", "dangerous", "recursive root deletion (rm -rf /)"),
    RedlineCase("x=$(rm -rf /)", "dangerous", "recursive root deletion (rm -rf /)"),
    RedlineCase("$(rm -rf /)", "dangerous", "recursive root deletion (rm -rf /)"),
    RedlineCase(
        "ls; chmod 777 /tmp/x",
        "dangerous",
        "world-writable permissions (chmod 777 /tmp/x)",
    ),
    RedlineCase(
        "rm -rf / && echo done", "dangerous", "recursive root deletion (rm -rf /)"
    ),
    RedlineCase('rm -rf "$(echo /)"', "dangerous", "recursive root deletion (rm -rf)"),
)

#: The grammar cannot read the line *and* a red line is in it: the conservative
#: fallback refuses.  An empty verdict here is a false negative.
UNPARSEABLE_DENY: tuple[RedlineCase, ...] = (
    RedlineCase('echo "rm -rf /', "unparseable_deny", "recursive root deletion ('rm -rf /')"),
    RedlineCase(
        'rm -rf / "unterminated', "unparseable_deny", "recursive root deletion (rm -rf /)"
    ),
    RedlineCase("rm -rf / &&", "unparseable_deny", "recursive root deletion (rm -rf /)"),
    RedlineCase("sudo ls &&", "unparseable_deny", "sudo escalation ('sudo ')"),
    RedlineCase(
        'echo "chmod 777 /tmp/x',
        "unparseable_deny",
        "world-writable permissions ('chmod 777')",
    ),
    RedlineCase(
        'mkfs /dev/sda1 "unterminated', "unparseable_deny", "filesystem format ('mkfs')"
    ),
    RedlineCase(
        "git push --force origin main &&",
        "unparseable_deny",
        "force git push ('git push --force')",
    ),
    RedlineCase("reboot &&", "unparseable_deny", "system reboot ('reboot')"),
    RedlineCase("cat .env &&", "unparseable_deny", "credential file (.env)"),
    RedlineCase(
        'dd if=/dev/sda of=/tmp/x "unclosed', "unparseable_deny", "disk write ('dd if=')"
    ),
    RedlineCase(
        "echo x > /dev/sda &&", "unparseable_deny", "raw disk write ('> /dev/sd')"
    ),
    RedlineCase(
        "chmod 777 /tmp/x &&", "unparseable_deny", "world-writable permissions (chmod 777)"
    ),
    RedlineCase('sh -c "rm -rf /', "unparseable_deny", "recursive root deletion ('rm -rf /')"),
    RedlineCase(
        "cat ~/.aws/credentials &&", "unparseable_deny", "credential file (.aws)"
    ),
)

#: The grammar cannot read the line and there is no red line in it: the line is
#: asked about, not refused.  The empty verdict is the existing contract.
UNPARSEABLE_WARN: tuple[RedlineCase, ...] = (
    RedlineCase("printf done &&", "unparseable_warn", ""),
    RedlineCase("cat >", "unparseable_warn", ""),
    RedlineCase("ls |", "unparseable_warn", ""),
    RedlineCase("echo a &&", "unparseable_warn", ""),
    RedlineCase('echo "unclosed', "unparseable_warn", ""),
    RedlineCase("printf '%s' \"sudo ", "unparseable_warn", ""),
    RedlineCase('echo "sudo ', "unparseable_warn", ""),
)

#: Ordinary work, and text that only looks like a red line.  A non-empty verdict
#: is a false positive.
SAFE: tuple[RedlineCase, ...] = (
    RedlineCase("ls -la", "safe", ""),
    RedlineCase("rm -rf ./build", "safe", ""),
    RedlineCase("rm -rf /tmp/build", "safe", ""),
    RedlineCase("grep -r TODO src/", "safe", ""),
    RedlineCase("git push origin main", "safe", ""),
    RedlineCase("chmod 644 script.sh", "safe", ""),
    RedlineCase("cat README.md", "safe", ""),
    RedlineCase("cat .env.example", "safe", ""),
    RedlineCase("cat src/config.py", "safe", ""),
    RedlineCase("grep -r password src/ tests/", "safe", ""),
    RedlineCase("dd if=/dev/zero of=/tmp/out.bin", "safe", ""),
    RedlineCase("mkfsdoc --help", "safe", ""),
    RedlineCase("echo shutdown", "safe", ""),
    RedlineCase('echo "rm -rf /"', "safe", ""),
    RedlineCase("echo 'rm -rf /'", "safe", ""),
    RedlineCase("# rm -rf /", "safe", ""),
    RedlineCase("cat notes.md  # do not rm -rf / here", "safe", ""),
    RedlineCase('grep -r "chmod 777" docs/', "safe", ""),
    RedlineCase('echo "sudo "', "safe", ""),
    RedlineCase("echo done", "safe", ""),
    RedlineCase("pwd", "safe", ""),
    RedlineCase("find . -name '*.py'", "safe", ""),
    RedlineCase("sed -n 1,5p README.md", "safe", ""),
    RedlineCase("python -m pytest -q", "safe", ""),
    RedlineCase("git status", "safe", ""),
    # A redirection with no command in it: nothing runs, so nothing is refused.
    RedlineCase("> out.txt", "safe", ""),
)

#: The grammar reads the line, no rule matches, and ``_grammar_reads`` then
#: returns ``""`` without consulting the text fallback -- the substring table
#: that *would* refuse these is never reached.  GuardFall's mechanism, in this
#: repository's own shape.  Reported, never failed; fixing one moves its count
#: to zero.
KNOWN_MISS: tuple[RedlineCase, ...] = (
    RedlineCase('x="rm -rf /"; $x', "known_miss", ""),
    RedlineCase("D=/data; rm -rf $D", "known_miss", ""),
    RedlineCase("rm -rf ${HOME}", "known_miss", ""),
    RedlineCase("rm -rf $HOME", "known_miss", ""),
    RedlineCase("rm -rf $PWD", "known_miss", ""),
    RedlineCase("rm -rf ./..", "known_miss", ""),
    RedlineCase("rm -rf ~", "known_miss", ""),
    RedlineCase("rm -rf ~/", "known_miss", ""),
    RedlineCase("rm -rf /home/user", "known_miss", ""),
    RedlineCase("rm -rf /var/log", "known_miss", ""),
    RedlineCase("cat ~/.ssh/id_$USER", "known_miss", ""),
    RedlineCase("x=1; $x rm -rf /", "known_miss", ""),
    # A wrapper the grammar peels only when the wrapper takes no value: ``env
    # -i`` and ``timeout 5`` put a word in front of sudo, and the chain stops
    # there.  ``doas`` is not in the wrapper table at all.
    RedlineCase("doas rm x", "known_miss", ""),
    RedlineCase("env -i sudo ls", "known_miss", ""),
    RedlineCase("timeout 5 sudo ls", "known_miss", ""),
    # The force flag spelled ``--force-with-lease`` is not ``--force``.
    RedlineCase(
        "git push --force-with-lease origin main", "known_miss", ""
    ),
)

#: Business-safe work the rules still refuse.  Reported, never failed.
KNOWN_FP: tuple[RedlineCase, ...] = (
    RedlineCase(
        "chmod 777 ./build", "known_fp", "world-writable permissions (chmod 777 ./build)"
    ),
    RedlineCase(
        "chmod 777 build", "known_fp", "world-writable permissions (chmod 777 build)"
    ),
    RedlineCase(
        "chmod 0777 ./build",
        "known_fp",
        "world-writable permissions (chmod 0777 ./build)",
    ),
    RedlineCase(
        "chmod a+rwx ./dist",
        "known_fp",
        "world-writable permissions (chmod a+rwx ./dist)",
    ),
    RedlineCase("sudo -u deploy ls", "known_fp", "sudo escalation (sudo -u deploy ls)"),
    RedlineCase("sudo echo hi", "known_fp", "sudo escalation (sudo echo hi)"),
    RedlineCase("reboot --help", "known_fp", "system reboot (reboot --help)"),
    RedlineCase("mkfs --help", "known_fp", "filesystem format (mkfs --help)"),
    RedlineCase(
        "mkfs.ext4 ./disk.img", "known_fp", "filesystem format (mkfs.ext4 ./disk.img)"
    ),
)

#: Every row, in class order.  The benchmark walks this and nothing else.
CORPUS: tuple[RedlineCase, ...] = (
    DANGEROUS + UNPARSEABLE_DENY + UNPARSEABLE_WARN + SAFE + KNOWN_MISS + KNOWN_FP
)
